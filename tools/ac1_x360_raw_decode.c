#include <direct.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "lzx.h"

static const unsigned char SIGNATURE[8] = {0x10, 0x04, 0xFA, 0x99, 0x57, 0xFB, 0xAA, 0x33};

static uint16_t read_be16(const unsigned char *data) {
    return (uint16_t)(((uint16_t)data[0] << 8) | data[1]);
}

static const unsigned char *find_signature(
    const unsigned char *cursor, const unsigned char *end) {
    while ((size_t)(end - cursor) >= sizeof(SIGNATURE)) {
        if (memcmp(cursor, SIGNATURE, sizeof(SIGNATURE)) == 0)
            return cursor;
        cursor++;
    }
    return NULL;
}

static const char *base_name(const char *path) {
    const char *slash = strrchr(path, '\\');
    const char *forward = strrchr(path, '/');
    if (!slash || (forward && forward > slash))
        slash = forward;
    return slash ? slash + 1 : path;
}

static int make_output_paths(
    const char *output_root, const char *input_path, char *directory, size_t directory_size) {
    char name[512];
    const char *input_name = base_name(input_path);
    if (strlen(input_name) >= sizeof(name))
        return 0;
    strcpy_s(name, sizeof(name), input_name);
    char *dot = strrchr(name, '.');
    if (dot)
        *dot = '\0';

    if (_mkdir(output_root) != 0 && errno != EEXIST)
        return 0;
    if (sprintf_s(directory, directory_size, "%s\\%s", output_root, name) < 0)
        return 0;
    if (_mkdir(directory) != 0 && errno != EEXIST)
        return 0;
    return 1;
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "Usage: %s INPUT_RAW_ENTRY OUTPUT_DIRECTORY\n", argv[0]);
        return 2;
    }

    FILE *input = fopen(argv[1], "rb");
    if (!input) {
        perror("fopen input");
        return 1;
    }
    _fseeki64(input, 0, SEEK_END);
    const __int64 signed_size = _ftelli64(input);
    _fseeki64(input, 0, SEEK_SET);
    if (signed_size < 0 || (uint64_t)signed_size > SIZE_MAX) {
        fprintf(stderr, "Unsupported input size\n");
        fclose(input);
        return 1;
    }
    const size_t input_size = (size_t)signed_size;
    unsigned char *data = malloc(input_size);
    if (!data || fread(data, 1, input_size, input) != input_size) {
        fprintf(stderr, "Could not read input\n");
        fclose(input);
        free(data);
        return 1;
    }
    fclose(input);

    char output_directory[1024];
    if (!make_output_paths(argv[2], argv[1], output_directory, sizeof(output_directory))) {
        fprintf(stderr, "Could not create output directory\n");
        free(data);
        return 1;
    }

    const unsigned char *end = data + input_size;
    const unsigned char *cursor = data;
    unsigned wrapper_index = 0;
    int status = 0;

    while ((cursor = find_signature(cursor, end)) != NULL) {
        const size_t signature_offset = (size_t)(cursor - data);
        if ((size_t)(end - cursor) < 17) {
            fprintf(stderr, "Truncated wrapper header at 0x%zX\n", signature_offset);
            status = 1;
            break;
        }

        const uint16_t version = read_be16(cursor + 8);
        const unsigned compression_type = cursor[10];
        const uint16_t chunk_count = read_be16(cursor + 15);
        if (version > 1 || compression_type != 3 || chunk_count == 0) {
            cursor++;
            continue;
        }

        const unsigned char *table = cursor + 17;
        if ((size_t)(end - table) < (size_t)chunk_count * 4) {
            fprintf(stderr, "Truncated chunk table at 0x%zX\n", signature_offset);
            status = 1;
            break;
        }
        const unsigned char *chunk_data = table + (size_t)chunk_count * 4;

        char output_path[1200];
        if (sprintf_s(
                output_path, sizeof(output_path), "%s\\%u.dat", output_directory, wrapper_index) <
            0) {
            status = 1;
            break;
        }
        FILE *output = fopen(output_path, "wb");
        if (!output) {
            perror("fopen output");
            status = 1;
            break;
        }

        printf("wrapper %u at 0x%zX: %u chunks -> %s\n",
               wrapper_index,
               signature_offset,
               chunk_count,
               output_path);

        for (unsigned i = 0; i < chunk_count; i++) {
            const uint16_t output_size = read_be16(table + (size_t)i * 4);
            const uint16_t stored_size = read_be16(table + (size_t)i * 4 + 2);
            if ((size_t)(end - chunk_data) < (size_t)stored_size + 4) {
                fprintf(stderr, "Truncated chunk %u in wrapper %u\n", i, wrapper_index);
                status = 1;
                break;
            }

            const unsigned char *stored = chunk_data + 4; /* skip CRC */
            if (stored_size == output_size) {
                if (fwrite(stored, 1, output_size, output) != output_size) {
                    status = 1;
                    break;
                }
            } else {
                size_t prefix_size;
                uint16_t framed_output_size;
                uint16_t compressed_size;
                if (stored[0] == 0xFF) {
                    if (stored_size < 5) {
                        status = 1;
                        break;
                    }
                    prefix_size = 5;
                    framed_output_size = read_be16(stored + 1);
                    compressed_size = read_be16(stored + 3);
                } else {
                    if (stored_size < 2) {
                        status = 1;
                        break;
                    }
                    prefix_size = 2;
                    framed_output_size = output_size;
                    compressed_size = read_be16(stored);
                }
                if (framed_output_size != output_size ||
                    prefix_size + compressed_size > stored_size) {
                    fprintf(stderr, "Invalid LZX frame in chunk %u\n", i);
                    status = 1;
                    break;
                }

                unsigned char *decoded = malloc(output_size);
                struct lzx_state *lzx = lzx_init(17);
                if (!decoded || !lzx) {
                    free(decoded);
                    if (lzx)
                        lzx_teardown(lzx);
                    status = 1;
                    break;
                }
                const int result = lzx_decompress(
                    lzx, stored + prefix_size, decoded, compressed_size, output_size);
                lzx_teardown(lzx);
                if (result != DECR_OK) {
                    fprintf(stderr,
                            "LZX error %d in wrapper %u chunk %u\n",
                            result,
                            wrapper_index,
                            i);
                    free(decoded);
                    status = 1;
                    break;
                }
                if (fwrite(decoded, 1, output_size, output) != output_size) {
                    free(decoded);
                    status = 1;
                    break;
                }
                free(decoded);
            }
            chunk_data += (size_t)stored_size + 4;
        }
        fclose(output);
        if (status)
            break;
        wrapper_index++;
        cursor = chunk_data;
    }

    if (!status && wrapper_index == 0) {
        fprintf(stderr, "No Xbox 360 AC1 compressed wrappers found\n");
        status = 1;
    }
    free(data);
    return status;
}
