#include "jpgd.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace
{
	struct image
	{
		int width;
		int height;
		int components;
		unsigned char* pixels;

		image() : width(0), height(0), components(0), pixels(nullptr) { }
		~image() { std::free(pixels); }

	private:
		image(const image&);
		image& operator=(const image&);

	public:
		image(image&& other) : width(other.width), height(other.height), components(other.components), pixels(other.pixels)
		{
			other.pixels = nullptr;
		}

		image& operator=(image&& other)
		{
			if (this != &other)
			{
				std::free(pixels);
				width = other.width;
				height = other.height;
				components = other.components;
				pixels = other.pixels;
				other.pixels = nullptr;
			}
			return *this;
		}
	};

	typedef std::chrono::steady_clock benchmark_clock;

	double elapsed_ms(benchmark_clock::time_point start)
	{
		return std::chrono::duration<double, std::milli>(benchmark_clock::now() - start).count();
	}

	image decode_full(const char* filename, int req_comps)
	{
		image result;
		int actual_comps = 0;
		result.pixels = jpgd::decompress_jpeg_image_from_file(filename, &result.width, &result.height, &actual_comps, req_comps);
		result.components = req_comps;
		return result;
	}

	image box_reference(const image& full, int denom, int req_comps)
	{
		image result;
		result.width = (full.width + denom - 1) / denom;
		result.height = (full.height + denom - 1) / denom;
		result.components = req_comps;
		result.pixels = static_cast<unsigned char*>(std::malloc(static_cast<size_t>(result.width) * result.height * req_comps));
		if (!result.pixels)
			return result;

		for (int oy = 0; oy < result.height; oy++)
		{
			for (int ox = 0; ox < result.width; ox++)
			{
				const int x0 = ox * denom;
				const int y0 = oy * denom;
				const int x1 = std::min(x0 + denom, full.width);
				const int y1 = std::min(y0 + denom, full.height);
				const int count = (x1 - x0) * (y1 - y0);
				for (int c = 0; c < req_comps; c++)
				{
					unsigned int sum = 0;
					for (int y = y0; y < y1; y++)
						for (int x = x0; x < x1; x++)
							sum += full.pixels[(static_cast<size_t>(y) * full.width + x) * req_comps + c];
					result.pixels[(static_cast<size_t>(oy) * result.width + ox) * req_comps + c] =
						static_cast<unsigned char>((sum + count / 2) / count);
				}
			}
		}
		return result;
	}

	image decode_scaled(const char* filename, int denom, int req_comps)
	{
		image result;
		int actual_comps = 0;
		result.pixels = jpgd::decompress_jpeg_image_scaled_from_file(filename, denom, &result.width, &result.height, &actual_comps, req_comps);
		result.components = req_comps;
		return result;
	}

	image decode_box(const char* filename, int denom, int req_comps)
	{
		image result;
		int actual_comps = 0;
		result.pixels = jpgd::decompress_jpeg_image_box_downsampled_from_file(filename, denom, &result.width, &result.height, &actual_comps, req_comps);
		result.components = req_comps;
		return result;
	}

	struct difference
	{
		int max_diff;
		double mismatch_percent;
		double mean_abs_error;
	};

	difference compare(const image& a, const image& b)
	{
		difference result = { -1, 0.0, 0.0 };
		if ((a.width != b.width) || (a.height != b.height) || (a.components != b.components) || (!a.pixels) || (!b.pixels))
			return result;

		const size_t count = static_cast<size_t>(a.width) * a.height * a.components;
		unsigned long long mismatches = 0;
		unsigned long long sum = 0;
		int max_diff = 0;
		for (size_t i = 0; i < count; i++)
		{
			const int d = std::abs(static_cast<int>(a.pixels[i]) - static_cast<int>(b.pixels[i]));
			if (d)
				mismatches++;
			if (d > max_diff)
				max_diff = d;
			sum += static_cast<unsigned int>(d);
		}
		result.max_diff = max_diff;
		result.mismatch_percent = count ? (100.0 * mismatches / count) : 0.0;
		result.mean_abs_error = count ? (static_cast<double>(sum) / count) : 0.0;
		return result;
	}

	double bench_baseline(const char* filename, int denom, int req_comps, int repeats)
	{
		benchmark_clock::time_point start = benchmark_clock::now();
		for (int i = 0; i < repeats; i++)
		{
			image full = decode_full(filename, req_comps);
			image box = box_reference(full, denom, req_comps);
			if (!full.pixels || !box.pixels)
				return -1.0;
		}
		return elapsed_ms(start) / repeats;
	}

	template <typename DecodeFunc>
	double bench_decode(DecodeFunc decode, const char* filename, int denom, int req_comps, int repeats)
	{
		benchmark_clock::time_point start = benchmark_clock::now();
		for (int i = 0; i < repeats; i++)
		{
			image decoded = decode(filename, denom, req_comps);
			if (!decoded.pixels)
				return -1.0;
		}
		return elapsed_ms(start) / repeats;
	}

	void usage(const char* exe)
	{
		std::printf("Usage: %s <file.jpg> [--comps 1|3|4] [--repeat N] [--scale 2|4|8]\n", exe);
		std::printf("       %s <file.jpg> --method baseline|scaled|box --scale N [--comps C] [--repeat N]\n", exe);
	}
}

int main(int argc, char** argv)
{
	if (argc < 2)
	{
		usage(argv[0]);
		return 1;
	}

	const char* filename = argv[1];
	int req_comps = 4;
	int repeats = 5;
	int only_scale = 0;
	const char* only_method = nullptr;

	for (int i = 2; i < argc; i++)
	{
		if ((!std::strcmp(argv[i], "--comps")) && (i + 1 < argc))
			req_comps = std::atoi(argv[++i]);
		else if ((!std::strcmp(argv[i], "--repeat")) && (i + 1 < argc))
			repeats = std::max(1, std::atoi(argv[++i]));
		else if ((!std::strcmp(argv[i], "--scale")) && (i + 1 < argc))
			only_scale = std::atoi(argv[++i]);
		else if ((!std::strcmp(argv[i], "--method")) && (i + 1 < argc))
			only_method = argv[++i];
		else
		{
			usage(argv[0]);
			return 1;
		}
	}

	if ((req_comps != 1) && (req_comps != 3) && (req_comps != 4))
	{
		std::fprintf(stderr, "Invalid --comps\n");
		return 1;
	}

	if (only_method && only_scale && ((only_scale != 2) && (only_scale != 4) && (only_scale != 8)))
	{
		std::fprintf(stderr, "Invalid --scale\n");
		return 1;
	}

	if (only_method)
	{
		double ms = -1.0;
		if (!std::strcmp(only_method, "baseline"))
			ms = bench_baseline(filename, only_scale, req_comps, repeats);
		else if (!std::strcmp(only_method, "scaled"))
			ms = bench_decode(decode_scaled, filename, only_scale, req_comps, repeats);
		else if (!std::strcmp(only_method, "box"))
			ms = bench_decode(decode_box, filename, only_scale, req_comps, repeats);
		else
		{
			usage(argv[0]);
			return 1;
		}

		if (ms < 0.0)
		{
			std::fprintf(stderr, "Decode failed\n");
			return 1;
		}

		image decoded = !std::strcmp(only_method, "scaled") ? decode_scaled(filename, only_scale, req_comps) :
			(!std::strcmp(only_method, "box") ? decode_box(filename, only_scale, req_comps) : decode_full(filename, req_comps));
		const size_t output_bytes = decoded.pixels ? static_cast<size_t>(decoded.width) * decoded.height * decoded.components : 0;
		std::printf("method=%s scale=1/%d comps=%d width=%d height=%d avg_ms=%.3f output_bytes=%zu\n",
			only_method, only_scale, req_comps, decoded.width, decoded.height, ms, output_bytes);
		return decoded.pixels ? 0 : 1;
	}

	image full = decode_full(filename, req_comps);
	if (!full.pixels)
	{
		std::fprintf(stderr, "Decode failed\n");
		return 1;
	}

	std::printf("image=%s full=%dx%d comps=%d repeat=%d\n", filename, full.width, full.height, req_comps, repeats);
	std::printf("scale,method,width,height,avg_ms,speedup_vs_full,output_bytes,max_diff_vs_box,mismatch_percent,mae\n");

	for (int denom = 2; denom <= 8; denom <<= 1)
	{
		if (only_scale && denom != only_scale)
			continue;

		const double full_ms = bench_baseline(filename, denom, req_comps, repeats);
		const double scaled_ms = bench_decode(decode_scaled, filename, denom, req_comps, repeats);
		const double box_ms = bench_decode(decode_box, filename, denom, req_comps, repeats);
		const image box = box_reference(full, denom, req_comps);
		const image scaled = decode_scaled(filename, denom, req_comps);
		const image exact = decode_box(filename, denom, req_comps);
		const difference scaled_diff = compare(scaled, box);
		const difference exact_diff = compare(exact, box);
		const size_t out_bytes = static_cast<size_t>(box.width) * box.height * req_comps;

		std::printf("1/%d,full+box,%d,%d,%.3f,1.00,%zu,%d,%.4f,%.4f\n",
			denom, box.width, box.height, full_ms, out_bytes, exact_diff.max_diff, exact_diff.mismatch_percent, exact_diff.mean_abs_error);
		std::printf("1/%d,scaled-idct,%d,%d,%.3f,%.2f,%zu,%d,%.4f,%.4f\n",
			denom, scaled.width, scaled.height, scaled_ms, scaled_ms > 0.0 ? full_ms / scaled_ms : 0.0, out_bytes,
			scaled_diff.max_diff, scaled_diff.mismatch_percent, scaled_diff.mean_abs_error);
		std::printf("1/%d,exact-box,%d,%d,%.3f,%.2f,%zu,%d,%.4f,%.4f\n",
			denom, exact.width, exact.height, box_ms, box_ms > 0.0 ? full_ms / box_ms : 0.0, out_bytes,
			exact_diff.max_diff, exact_diff.mismatch_percent, exact_diff.mean_abs_error);
	}

	return 0;
}
