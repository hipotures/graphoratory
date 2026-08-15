#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

__extension__ using Bits = unsigned __int128;

constexpr std::uint16_t kProtocolVersion = 2;
constexpr std::uint16_t kCommandScore = 1;
constexpr std::uint16_t kCommandQuit = 2;
constexpr std::uint16_t kStatusOk = 0;
constexpr std::uint16_t kStatusError = 1;
constexpr std::uint16_t kStatusDominated = 2;
constexpr std::uint32_t kMaximumPayloadBytes = 64U * 1024U;
constexpr std::uint32_t kCutoffFlag = 1U;
constexpr std::uint32_t kCompactDominatedFlag = 4U;
constexpr std::uint32_t kKnownRequestFlags = 7U;

struct Request {
    std::uint64_t request_id = 0;
    std::uint16_t order = 0;
    std::uint16_t word_count = 0;
    std::uint32_t limit = 0;
    std::uint32_t node_budget = 0;
    std::uint32_t flags = 0;
    std::uint32_t cutoff_total = 0;
    std::uint32_t cutoff_weighted = 0;
    std::uint32_t cutoff_simplicity = 0;
    bool cutoff_inclusive = false;
    std::vector<std::uint16_t> lengths;
    std::vector<Bits> rows;
};

struct CountResult {
    std::uint16_t length = 0;
    std::uint32_t count = 0;
    bool complete = false;
    std::uint64_t nodes = 0;
    std::uint64_t elapsed_ns = 0;
    bool cutoff_reached = false;
};

std::uint16_t read_u16() {
    unsigned char bytes[2]{};
    std::cin.read(reinterpret_cast<char*>(bytes), sizeof(bytes));
    if (!std::cin) {
        throw std::runtime_error("truncated u16");
    }
    return static_cast<std::uint16_t>(
        static_cast<std::uint16_t>(bytes[0])
        | (static_cast<std::uint16_t>(bytes[1]) << 8U));
}

std::uint32_t read_u32() {
    unsigned char bytes[4]{};
    std::cin.read(reinterpret_cast<char*>(bytes), sizeof(bytes));
    if (!std::cin) {
        throw std::runtime_error("truncated u32");
    }
    return static_cast<std::uint32_t>(bytes[0])
        | (static_cast<std::uint32_t>(bytes[1]) << 8U)
        | (static_cast<std::uint32_t>(bytes[2]) << 16U)
        | (static_cast<std::uint32_t>(bytes[3]) << 24U);
}

std::uint64_t read_u64() {
    unsigned char bytes[8]{};
    std::cin.read(reinterpret_cast<char*>(bytes), sizeof(bytes));
    if (!std::cin) {
        throw std::runtime_error("truncated u64");
    }
    std::uint64_t value = 0;
    for (unsigned int index = 0; index < 8; ++index) {
        value |= static_cast<std::uint64_t>(bytes[index]) << (index * 8U);
    }
    return value;
}

void write_u16(std::uint16_t value) {
    const unsigned char bytes[2] = {
        static_cast<unsigned char>(value & 0xffU),
        static_cast<unsigned char>((value >> 8U) & 0xffU),
    };
    std::cout.write(reinterpret_cast<const char*>(bytes), sizeof(bytes));
}

void write_u32(std::uint32_t value) {
    unsigned char bytes[4]{};
    for (unsigned int index = 0; index < 4; ++index) {
        bytes[index] = static_cast<unsigned char>(
            (value >> (index * 8U)) & 0xffU);
    }
    std::cout.write(reinterpret_cast<const char*>(bytes), sizeof(bytes));
}

void write_u64(std::uint64_t value) {
    unsigned char bytes[8]{};
    for (unsigned int index = 0; index < 8; ++index) {
        bytes[index] = static_cast<unsigned char>(
            (value >> (index * 8U)) & 0xffU);
    }
    std::cout.write(reinterpret_cast<const char*>(bytes), sizeof(bytes));
}

CountResult count_cycles(
    const std::vector<Bits>& rows,
    std::uint16_t order,
    std::uint16_t length,
    std::uint32_t limit,
    std::uint32_t node_budget,
    std::vector<Bits>& seen_at,
    std::vector<Bits>& available_at,
    std::uint32_t stop_at_count) {
    const auto started = std::chrono::steady_clock::now();
    CountResult result;
    result.length = length;
    bool budget_exhausted = false;

    for (std::uint16_t start = 0; start < order; ++start) {
        const Bits start_bit = Bits{1} << start;
        const Bits greater_mask =
            start >= 127 ? Bits{0} : ~((Bits{1} << (start + 1U)) - 1U);
        seen_at[0] = start_bit;
        available_at[0] = rows[start] & greater_mask;
        ++result.nodes;
        if (node_budget != 0 && result.nodes > node_budget) {
            budget_exhausted = true;
            break;
        }

        std::uint16_t depth = 1;
        int first_neighbor = -1;
        while (depth != 0) {
            const std::uint16_t parent_index = depth - 1U;
            const Bits bits = available_at[parent_index];
            if (bits == 0) {
                --depth;
                continue;
            }
            const Bits bit = bits & (~bits + 1U);
            available_at[parent_index] = bits ^ bit;
            const std::uint64_t low = static_cast<std::uint64_t>(bit);
            const int vertex = (
                low != 0
                ? __builtin_ctzll(low)
                : 64 + __builtin_ctzll(
                    static_cast<std::uint64_t>(bit >> 64U)));
            if (depth == 1) {
                first_neighbor = vertex;
            }
            const Bits seen = seen_at[parent_index] | bit;
            seen_at[depth] = seen;
            ++result.nodes;
            if (node_budget != 0 && result.nodes > node_budget) {
                budget_exhausted = true;
                break;
            }

            const std::uint16_t child_depth = depth + 1U;
            if (child_depth == length) {
                if ((rows[vertex] & start_bit) != 0
                    && first_neighbor < vertex) {
                    ++result.count;
                    if (stop_at_count != 0
                        && result.count >= stop_at_count) {
                        result.cutoff_reached = true;
                        break;
                    }
                    if (result.count >= limit) {
                        break;
                    }
                }
                continue;
            }
            available_at[depth] = rows[vertex] & ~seen & greater_mask;
            depth = child_depth;
        }
        if (result.count >= limit
            || budget_exhausted
            || result.cutoff_reached) {
            break;
        }
    }

    result.complete = !budget_exhausted
        && !result.cutoff_reached
        && result.count < limit;
    result.elapsed_ns = static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - started).count());
    return result;
}

Request read_request(
    std::uint16_t command,
    std::uint32_t payload_bytes) {
    Request request;
    request.request_id = read_u64();
    request.order = read_u16();
    request.word_count = read_u16();
    request.limit = read_u32();
    request.node_budget = read_u32();
    request.flags = read_u32();
    request.cutoff_total = read_u32();
    request.cutoff_weighted = read_u32();
    request.cutoff_simplicity = read_u32();
    request.cutoff_inclusive = read_u32() != 0;
    const std::uint16_t length_count = read_u16();
    const std::uint16_t reserved = read_u16();
    if (command != kCommandScore) {
        throw std::runtime_error("unsupported command");
    }
    if (request.order < 4 || request.order > 128) {
        throw std::runtime_error("order must be between 4 and 128");
    }
    const std::uint16_t expected_words =
        static_cast<std::uint16_t>((request.order + 63U) / 64U);
    if (request.word_count != expected_words) {
        throw std::runtime_error("word count does not match order");
    }
    if (request.limit < 2
        || request.node_budget == 0
        || (request.flags & ~kKnownRequestFlags) != 0
        || (
            (request.flags & kCompactDominatedFlag) != 0
            && (request.flags & kCutoffFlag) == 0)
        || length_count > 64
        || reserved != 0) {
        throw std::runtime_error("invalid score request");
    }
    const std::uint32_t expected_payload =
        static_cast<std::uint32_t>(length_count) * sizeof(std::uint16_t)
        +
        static_cast<std::uint32_t>(request.order)
        * request.word_count * sizeof(std::uint64_t);
    if (payload_bytes != expected_payload
        || payload_bytes > kMaximumPayloadBytes) {
        throw std::runtime_error("invalid adjacency payload size");
    }
    request.lengths.reserve(length_count);
    for (std::uint16_t index = 0; index < length_count; ++index) {
        const std::uint16_t length = read_u16();
        if (length < 3
            || length > request.order
            || (!request.lengths.empty()
                && length <= request.lengths.back())) {
            throw std::runtime_error("invalid cycle lengths");
        }
        request.lengths.push_back(length);
    }
    request.rows.assign(request.order, 0);
    for (std::uint16_t row = 0; row < request.order; ++row) {
        Bits value = read_u64();
        if (request.word_count == 2) {
            value |= Bits{read_u64()} << 64U;
        }
        request.rows[row] = value;
    }
    return request;
}

bool cutoff_reached(
    const Request& request,
    std::uint32_t total,
    std::uint32_t weighted,
    std::uint32_t simplicity) {
    if ((request.flags & 1U) == 0) {
        return false;
    }
    const bool greater =
        total > request.cutoff_total
        || (
            total == request.cutoff_total
            && (
                weighted > request.cutoff_weighted
                || (
                    weighted == request.cutoff_weighted
                    && simplicity > request.cutoff_simplicity)));
    const bool equal =
        total == request.cutoff_total
        && weighted == request.cutoff_weighted
        && simplicity == request.cutoff_simplicity;
    return greater || (request.cutoff_inclusive && equal);
}

void write_response(
    std::uint64_t request_id,
    std::uint16_t status,
    const std::vector<CountResult>& results) {
    std::cout.write("SGSR", 4);
    write_u16(kProtocolVersion);
    write_u16(status);
    write_u64(request_id);
    write_u16(static_cast<std::uint16_t>(results.size()));
    write_u16(0);
    write_u32(static_cast<std::uint32_t>(results.size() * 24U));
    for (const CountResult& result : results) {
        write_u16(result.length);
        std::cout.put(result.complete ? '\x01' : '\x00');
        std::cout.put('\x00');
        write_u32(result.count);
        write_u64(result.nodes);
        write_u64(result.elapsed_ns);
    }
    std::cout.flush();
}

int serve() {
    std::vector<Bits> seen_at(128, 0);
    std::vector<Bits> available_at(128, 0);
    while (true) {
        char magic[4]{};
        std::cin.read(magic, sizeof(magic));
        if (std::cin.eof() && std::cin.gcount() == 0) {
            return 0;
        }
        if (!std::cin || std::memcmp(magic, "SGSC", 4) != 0) {
            return 1;
        }
        const std::uint16_t version = read_u16();
        const std::uint16_t command = read_u16();
        const std::uint32_t payload_bytes = read_u32();
        if (version != kProtocolVersion
            || payload_bytes > kMaximumPayloadBytes) {
            return 1;
        }
        if (command == kCommandQuit) {
            return payload_bytes == 0 ? 0 : 1;
        }
        Request request;
        try {
            request = read_request(command, payload_bytes);
            std::vector<CountResult> results;
            std::uint32_t partial_total = 0;
            std::uint32_t partial_weighted = 0;
            std::uint32_t simplicity = 0;
            for (const Bits row : request.rows) {
                simplicity += static_cast<std::uint32_t>(
                    __builtin_popcountll(static_cast<std::uint64_t>(row))
                    + __builtin_popcountll(
                        static_cast<std::uint64_t>(row >> 64U)));
            }
            simplicity /= 2U;
            std::uint16_t response_status = kStatusOk;
            // Partial penalties only increase, so cutoff proofs are
            // independent of evaluation order.
            for (
                std::size_t evaluation_index = 0;
                evaluation_index < request.lengths.size();
                ++evaluation_index
            ) {
                const std::size_t length_index =
                    (request.flags & 2U) != 0
                    ? request.lengths.size() - 1U - evaluation_index
                    : evaluation_index;
                const std::uint16_t length =
                    request.lengths[length_index];
                if (cutoff_reached(
                        request,
                        partial_total,
                        partial_weighted,
                        simplicity)) {
                    response_status = kStatusDominated;
                    break;
                }
                const std::uint32_t cap = request.limit - 1U;
                const std::uint32_t weight =
                    std::max(1U, 64U / length);
                std::uint32_t stop_at_count = 0;
                if ((request.flags & 1U) != 0) {
                    for (std::uint32_t count = 1;
                         count <= request.limit;
                         ++count) {
                        const std::uint32_t bounded =
                            std::min(count, cap);
                        if (cutoff_reached(
                                request,
                                partial_total + bounded,
                                partial_weighted + bounded * weight,
                                simplicity)) {
                            stop_at_count = count;
                            break;
                        }
                    }
                }
                CountResult result = count_cycles(
                    request.rows,
                    request.order,
                    length,
                    request.limit,
                    request.node_budget,
                    seen_at,
                    available_at,
                    stop_at_count);
                partial_total += std::min(result.count, cap);
                partial_weighted += (
                    std::min(result.count, cap) * weight);
                results.push_back(result);
                if (result.cutoff_reached) {
                    response_status = kStatusDominated;
                    break;
                }
            }
            if (
                response_status == kStatusDominated
                && (request.flags & kCompactDominatedFlag) != 0) {
                results.clear();
            } else {
                std::sort(
                    results.begin(),
                    results.end(),
                    [](const CountResult& left, const CountResult& right) {
                        return left.length < right.length;
                    });
            }
            write_response(request.request_id, response_status, results);
        } catch (const std::exception&) {
            write_response(request.request_id, kStatusError, {});
        }
    }
}

}  // namespace

int main(int argc, char** argv) {
    if (argc == 2 && std::string(argv[1]) == "--version") {
        std::cout << "sglab-score-worker 2\n";
        return 0;
    }
    if (argc == 2 && std::string(argv[1]) == "--serve") {
        std::ios::sync_with_stdio(false);
        return serve();
    }
    std::cerr << "usage: sglab-score-worker --serve|--version\n";
    return 2;
}
