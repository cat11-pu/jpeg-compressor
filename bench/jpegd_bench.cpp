#include "jpgd.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#include <sys/resource.h>

#ifdef BENCH_TRACK_MALLOC
static size_t heap_current;
static size_t heap_peak;

static void heap_add(size_t size)
{
	heap_current += size;
	if (heap_current > heap_peak)
		heap_peak = heap_current;
}

extern "C" void* __real_malloc(size_t size);
extern "C" void* __real_calloc(size_t count, size_t size);
extern "C" void* __real_realloc(void* ptr, size_t size);
extern "C" void __real_free(void* ptr);

extern "C" void* __wrap_malloc(size_t size)
{
	unsigned char* base = static_cast<unsigned char*>(__real_malloc(size + 16));
	if (!base)
		return nullptr;
	*reinterpret_cast<size_t*>(base) = size;
	heap_add(size);
	return base + 16;
}

extern "C" void* __wrap_calloc(size_t count, size_t size)
{
	const size_t total = count * size;
	unsigned char* base = static_cast<unsigned char*>(__real_calloc(1, total + 16));
	if (!base)
		return nullptr;
	*reinterpret_cast<size_t*>(base) = total;
	heap_add(total);
	return base + 16;
}

extern "C" void* __wrap_realloc(void* ptr, size_t size)
{
	if (!ptr)
		return __wrap_malloc(size);
	unsigned char* old_base = static_cast<unsigned char*>(ptr) - 16;
	const size_t old_size = *reinterpret_cast<size_t*>(old_base);
	unsigned char* base = static_cast<unsigned char*>(__real_realloc(old_base, size + 16));
	if (!base)
		return nullptr;
	*reinterpret_cast<size_t*>(base) = size;
	heap_current -= old_size;
	heap_add(size);
	return base + 16;
}

extern "C" void __wrap_free(void* ptr)
{
	if (!ptr)
		return;
	unsigned char* base = static_cast<unsigned char*>(ptr) - 16;
	heap_current -= *reinterpret_cast<size_t*>(base);
	__real_free(base);
}
#endif

static double now_ms()
{
	return std::chrono::duration<double, std::milli>(
		std::chrono::steady_clock::now().time_since_epoch()).count();
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
	if (size <= 0)
	{
		fclose(file);
		return false;
	}

	rewind(file);
	data.resize(static_cast<size_t>(size));
	const bool ok = fread(data.data(), 1, data.size(), file) == data.size();
	fclose(file);
	return ok;
}

static bool write_file(const char* path, const unsigned char* data, size_t size)
{
	FILE* file = fopen(path, "wb");
	if (!file)
		return false;
	const bool ok = fwrite(data, 1, size, file) == size;
	fclose(file);
	return ok;
}

int main(int argc, char** argv)
{
	if ((argc < 4) || (argc > 6))
	{
		fprintf(stderr, "usage: %s <input.jpg> <output.rgb> <iterations> [flags] [req_comps]\n", argv[0]);
		return 2;
	}

	const char* input_path = argv[1];
	const char* output_path = argv[2];
	const int iterations = atoi(argv[3]);
	const uint32_t flags = (argc >= 5) ? static_cast<uint32_t>(strtoul(argv[4], nullptr, 0)) : 0;
	const int req_comps = (argc >= 6) ? atoi(argv[5]) : 3;

	if ((iterations < 1) || ((req_comps != 1) && (req_comps != 3) && (req_comps != 4)))
		return 2;

	std::vector<unsigned char> source;
	if (!read_file(input_path, source))
	{
		fprintf(stderr, "failed to read %s\n", input_path);
		return 1;
	}

	int width = 0;
	int height = 0;
	int comps = 0;
	unsigned char* output = jpgd::decompress_jpeg_image_from_memory(
		source.data(), static_cast<int>(source.size()), &width, &height, &comps, req_comps, flags);
	if (!output)
	{
		fprintf(stderr, "decode failed\n");
		return 1;
	}
	free(output);

	std::vector<double> times;
	times.reserve(static_cast<size_t>(iterations));

	for (int iteration = 0; iteration < iterations; ++iteration)
	{
		const double start = now_ms();
		output = jpgd::decompress_jpeg_image_from_memory(
			source.data(), static_cast<int>(source.size()), &width, &height, &comps, req_comps, flags);
		const double elapsed = now_ms() - start;
		if (!output)
		{
			fprintf(stderr, "decode failed at iteration %d\n", iteration);
			return 1;
		}
		times.push_back(elapsed);
		free(output);
	}

	const size_t output_size = static_cast<size_t>(width) * static_cast<size_t>(height) *
		static_cast<size_t>(req_comps);
	output = jpgd::decompress_jpeg_image_from_memory(
		source.data(), static_cast<int>(source.size()), &width, &height, &comps, req_comps, flags);
	if (!output || !write_file(output_path, output, output_size))
	{
		free(output);
		fprintf(stderr, "failed to write %s\n", output_path);
		return 1;
	}
	free(output);

	std::vector<double> sorted = times;
	std::sort(sorted.begin(), sorted.end());
	double sum = 0.0;
	for (double value : times)
		sum += value;

	struct rusage usage;
	getrusage(RUSAGE_SELF, &usage);

	printf("resolution=%dx%d pixels=%lld comps=%d iterations=%d best_ms=%.3f median_ms=%.3f avg_ms=%.3f peak_rss_kb=%ld",
		width, height, static_cast<long long>(width) * height, req_comps, iterations,
		sorted.front(), sorted[sorted.size() / 2], sum / times.size(), usage.ru_maxrss);
#ifdef BENCH_TRACK_MALLOC
	printf(" peak_heap_kb=%zu", heap_peak / 1024);
#endif
	printf("\n");
	return 0;
}
