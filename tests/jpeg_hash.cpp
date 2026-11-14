#include "jpgd.h"

#include <cstdio>
#include <cstdlib>
#include <vector>

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

int main(int argc, char** argv)
{
	if (argc != 2)
	{
		fprintf(stderr, "usage: %s <jpeg>\n", argv[0]);
		return 2;
	}

	std::vector<unsigned char> data;
	if (!read_file(argv[1], data))
		return 2;

	int width = 0;
	int height = 0;
	int components = 0;
	unsigned char* image = jpgd::decompress_jpeg_image_from_memory(data.data(),
		static_cast<int>(data.size()), &width, &height, &components, 3);
	if (!image)
		return 1;

	uint64_t hash = UINT64_C(1469598103934665603);
	const size_t size = static_cast<size_t>(width) * static_cast<size_t>(height) * 3;
	for (size_t i = 0; i < size; ++i)
	{
		hash ^= image[i];
		hash *= UINT64_C(1099511628211);
	}

	printf("OK|%d|%d|%d|%016llx\n", width, height, components,
		static_cast<unsigned long long>(hash));
	free(image);
	return 0;
}
