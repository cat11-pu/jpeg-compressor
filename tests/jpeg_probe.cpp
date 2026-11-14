#include "jpgd.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

static uint64_t hash_bytes(uint64_t hash, const unsigned char* data, size_t size)
{
	for (size_t i = 0; i < size; ++i)
	{
		hash ^= data[i];
		hash *= UINT64_C(1099511628211);
	}
	return hash;
}

static bool read_file(const char* path, std::vector<unsigned char>& data)
{
	FILE* file = fopen(path, "rb");
	if (!file)
		return false;
	if (fseek(file, 0, SEEK_END) != 0)
	{
		fclose(file);
		return false;
	}
	const long size = ftell(file);
	if (size < 0 || fseek(file, 0, SEEK_SET) != 0)
	{
		fclose(file);
		return false;
	}
	data.resize(static_cast<size_t>(size));
	const size_t read_size = data.empty() ? 0 : fread(data.data(), 1, data.size(), file);
	fclose(file);
	return read_size == data.size();
}

static void print_decoder_error(const char* prefix, const jpgd::jpeg_decoder& decoder)
{
	printf("%s_ERR|%d|%lld|%s\n", prefix, static_cast<int>(decoder.get_error_code()),
		static_cast<long long>(decoder.get_error_offset()), decoder.get_error_message());
}

static bool probe_class(const std::vector<unsigned char>& data, int max_rows)
{
	jpgd::jpeg_decoder_mem_stream stream(data.data(), static_cast<uint>(data.size()));
	jpgd::jpeg_decoder decoder(&stream);
	if (decoder.get_error_code() != jpgd::JPGD_SUCCESS)
	{
		print_decoder_error("CLASS", decoder);
		return false;
	}
	if (decoder.begin_decoding() != jpgd::JPGD_SUCCESS)
	{
		print_decoder_error("CLASS", decoder);
		return false;
	}

	const int width = decoder.get_width();
	const int height = decoder.get_height();
	const int components = decoder.get_num_components();
	const int bytes_per_pixel = decoder.get_bytes_per_pixel();
	uint64_t hash = UINT64_C(1469598103934665603);
	int rows = 0;

	while (true)
	{
		const void* scan_line = nullptr;
		uint scan_line_len = 0;
		const int status = decoder.decode(&scan_line, &scan_line_len);
		if (status == jpgd::JPGD_DONE)
			break;
		if (status != jpgd::JPGD_SUCCESS)
		{
			print_decoder_error("CLASS", decoder);
			return false;
		}
		if (!scan_line || scan_line_len != static_cast<uint>(width * bytes_per_pixel))
		{
			printf("CLASS_ERR|INVALID_SCANLINE|%lld|scanline shape mismatch at row %d\n",
				static_cast<long long>(decoder.get_total_bytes_read()), rows);
			return false;
		}
		if (max_rows > 0 && rows >= max_rows)
			break;
		hash = hash_bytes(hash, static_cast<const unsigned char*>(scan_line), scan_line_len);
		++rows;
	}

	printf("CLASS_OK|%d|%d|%d|%d|%016llx|%d\n", width, height, components, bytes_per_pixel,
		static_cast<unsigned long long>(hash), rows);
	return true;
}

static bool probe_wrapper(const std::vector<unsigned char>& data)
{
	int width = 0;
	int height = 0;
	int components = 0;
	unsigned char* image = jpgd::decompress_jpeg_image_from_memory(data.data(),
		static_cast<int>(data.size()), &width, &height, &components, 3);
	if (!image)
	{
		printf("WRAP_ERR|null\n");
		return false;
	}

	const size_t size = static_cast<size_t>(width) * static_cast<size_t>(height) * 3;
	const uint64_t hash = hash_bytes(UINT64_C(1469598103934665603), image, size);
	printf("WRAP_OK|%d|%d|%d|%016llx\n", width, height, components,
		static_cast<unsigned long long>(hash));
	free(image);
	return true;
}

int main(int argc, char** argv)
{
	if (argc < 2)
	{
		fprintf(stderr, "usage: %s <jpeg> [--max-rows N]\n", argv[0]);
		return 2;
	}

	int max_rows = 0;
	for (int i = 2; i + 1 < argc; ++i)
	{
		if (strcmp(argv[i], "--max-rows") == 0)
			max_rows = atoi(argv[i + 1]);
	}

	std::vector<unsigned char> data;
	if (!read_file(argv[1], data))
	{
		fprintf(stderr, "failed to read %s\n", argv[1]);
		return 2;
	}

	probe_class(data, max_rows);
	probe_wrapper(data);
	return 0;
}
