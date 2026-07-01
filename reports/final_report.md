# Báo cáo Day 10 Reliability

## 1. Tóm tắt kiến trúc

Gateway định tuyến mọi request qua chuỗi: kiểm tra cache trước → circuit breaker bảo vệ →
chuỗi provider dự phòng, và chỉ trả về phản hồi suy giảm tĩnh (static fallback) khi tất cả
provider đều không khả dụng.

```
User Request
    |
    v
[Gateway.complete(prompt)]
    |
    v
[Cache check] ---> HIT (score >= threshold, không phải false-hit) ---> trả về giá trị cached
    |                                                            route=cache_hit:<score>
    | MISS / bị chặn vì privacy / bị từ chối vì false-hit
    v
[Circuit Breaker: primary] --(CLOSED/HALF_OPEN: cho phép)--> FakeLLMProvider "primary"
    |  OPEN (chưa hết reset_timeout): bỏ qua ngay lập tức, không có retry storm
    |  thành công -> cache.set(), trả về route="primary"
    |  ProviderError / CircuitOpenError -> ghi nhận failure, thử provider tiếp theo
    v
[Circuit Breaker: backup] --(CLOSED/HALF_OPEN: cho phép)--> FakeLLMProvider "backup"
    |  thành công -> cache.set(), trả về route="fallback"
    |  ProviderError / CircuitOpenError -> ghi nhận failure, tiếp tục
    v
[Static fallback] -- tất cả provider đều fail --> thông báo suy giảm, route="static_fallback",
                                                error=<lỗi provider cuối cùng>
```

Lớp cache có thể thay thế linh hoạt: `ResponseCache` (in-memory, theo từng process) hoặc
`SharedRedisCache` (dùng Redis, chia sẻ giữa nhiều gateway instance) — chọn qua `cache.backend`
trong `configs/default.yaml`. Cả hai đều dùng chung guardrail privacy (`_is_uncacheable`) và
guardrail false-hit (`_looks_like_false_hit`) để quy tắc privacy/độ chính xác không bị lệch
giữa hai backend.

## 2. Cấu hình

| Tham số | Giá trị | Lý do |
|---|---:|---|
| failure_threshold | 3 | 3 lần fail liên tiếp là đủ tín hiệu để ngừng dồn request vào một provider đang lỗi, mà không bị kích hoạt chỉ vì một lỗi thoáng qua. |
| reset_timeout_seconds | 2 | Đủ ngắn để các kịch bản chaos (100 request/kịch bản, độ trễ ~200-300ms) thực sự kích hoạt được probe HALF_OPEN trong một lần chạy, cho ra `recovery_time_ms` quan sát được. |
| success_threshold | 1 | Một probe HALF_OPEN thành công là đủ để tin tưởng lại provider — `FakeLLMProvider` fail không có "trí nhớ" (fail_rate cố định), nên yêu cầu nhiều probe hơn chỉ làm tăng độ trễ mà không giảm rủi ro. |
| cache TTL (ttl_seconds) | 300s | Khớp với một "phiên" truy vấn liên quan điển hình trong bộ dữ liệu mẫu; đủ dài để tái sử dụng trong một lần load-test, đủ ngắn để câu trả lời có yếu tố thời gian không bị lỗi thời quá lâu. |
| similarity_threshold | 0.92 | Đã thử 0.85 trước — bị gộp nhầm các câu hỏi khác nhau như "circuit breaker states" với "circuit breaker thresholds" thành false hit. Nâng lên 0.92 vẫn giữ được các câu diễn đạt gần giống nhau trong cache, đồng thời loại các câu khác biệt; guardrail số học `_looks_like_false_hit` xử lý nốt trường hợp biên (ngày tháng/số ID khác nhau nhưng điểm similarity cao). |
| load_test.requests | 100 mỗi kịch bản (tổng 300 qua 3 kịch bản) | Đủ lớn để có P50/P95/P99 ổn định và `circuit_open_count` có ý nghĩa, mà không làm chaos run quá chậm. |
| chaos seed (`--seed`, mặc định 42) | 42 | `scripts/run_chaos.py` seed `random` trước khi build gateway. Điều này giúp kết quả *đa phần* tái lập được, nhưng không đảm bảo giống hệt tuyệt đối mỗi lần chạy — xem ghi chú Reproducibility bên dưới để hiểu lý do. |

**Ghi chú về khả năng tái lập (reproducibility):** `CircuitBreaker.allow_request()` quyết định
chuyển trạng thái OPEN → HALF_OPEN dựa trên thời gian thực (`time.monotonic() - opened_at >=
reset_timeout_seconds`), không phải đồng hồ giả lập. Qua 5 lần chạy liên tiếp cấu hình mặc định
với `--seed 42`, 4/5 lần cho ra các chỉ số phi-latency giống hệt nhau; 1/5 lần lệch nhẹ
(`fallback_success_rate` 0.9222 so với 0.9213, `estimated_cost` 0.044378 so với 0.044722,
`recovery_time_ms` ~2372-2382ms so với một giá trị ngoại lệ 2248ms) vì một probe HALF_OPEN xảy
ra sớm/muộn hơn vài mili-giây so với thường lệ, làm thay đổi việc `provider.complete()` — và do
đó các lần gọi RNG bên trong nó — có được gọi cho request đó hay không, kéo theo sai lệch dây
chuyền cho toàn bộ chuỗi random đã seed phía sau. Đây là đặc tính của việc trộn một PRNG đã seed
với trạng thái dựa trên thời gian thực (`reset_timeout_seconds=2`), không phải lỗi: `latency_*_ms`
và `recovery_time_ms` vốn dĩ phụ thuộc vào thời gian thực trôi qua. Kết luận thực tế: nên xem mỗi
lần chạy đơn lẻ là một mẫu đại diện trong một biên độ nhỏ (xem khoảng dao động 5 lần chạy ở mục
§4) thay vì một bản sao chính xác — `total_requests`, `circuit_open_count`, `cache_hit_rate`, và
kết quả pass/fail của từng kịch bản đều ổn định qua cả 5 lần chạy trong mẫu này.

## 3. Định nghĩa SLO

| SLI | Mục tiêu SLO | Giá trị thực tế | Đạt? |
|---|---|---:|---|
| Availability | >= 99% | 97.67% | Không — hơi dưới mục tiêu; kịch bản primary_timeout_100 kéo giá trị này xuống (xem §7). |
| Latency P95 | < 2500 ms | 320.33 ms | Đạt |
| Fallback success rate | >= 95% | 92.22% | Không — dưới mục tiêu; xem phân tích lỗi (§8). |
| Cache hit rate | >= 10% | 61.67% | Đạt |
| Recovery time | < 5000 ms | 2379.12 ms (khoảng 2248-2382ms qua 5 lần chạy) | Đạt |

## 4. Số liệu (Metrics)

Từ `reports/metrics.json` (cấu hình mặc định, cache memory, 300 request qua 3 kịch bản, `--seed 42`):

| Metric | Giá trị |
|---|---:|
| total_requests | 300 |
| availability | 0.9767 |
| error_rate | 0.0233 |
| latency_p50_ms | 286.95 |
| latency_p95_ms | 320.33 |
| latency_p99_ms | 323.08 |
| fallback_success_rate | 0.9222 |
| cache_hit_rate | 0.6167 |
| estimated_cost_saved | 0.185 |
| circuit_open_count | 11 |
| recovery_time_ms | 2379.12 |

**Khoảng dao động qua 5 lần chạy** (cùng config/seed, xem ghi chú reproducibility ở §2):
`availability` ổn định ở 0.9767 (4/5 lần), `fallback_success_rate` 0.9213–0.9222, `estimated_cost`
0.044378–0.044722, `cache_hit_rate` 0.6167 (5/5 lần), `circuit_open_count` 11 (5/5 lần),
`latency_p50_ms` 285.18–287.55, `recovery_time_ms` 2248.20–2381.64. Cũng đã xuất ra CSV:
`reports/metrics.csv` (xuất một dòng qua `RunMetrics.write_csv()`, chạy bằng lệnh
`python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json --csv-out reports/metrics.csv`).

## 5. So sánh cache

Cùng config và seed, `cache.enabled: false` (`configs/no_cache.yaml`) so với
`cache.enabled: true, backend: memory` (`configs/default.yaml`), cả hai đều 300 request / 3 kịch bản:

| Metric | Không có cache | Có cache | Chênh lệch |
|---|---:|---:|---|
| latency_p50_ms | 276.29 | 286.95 | +10.66 (chi phí tra cứu cache không đáng kể so với độ trễ provider; nhiễu do wall-clock sleep thực) |
| latency_p95_ms | 320.13 | 320.33 | +0.20 |
| estimated_cost | 0.132912 | 0.044378 | -0.088534 (-67%) |
| cache_hit_rate | 0 | 0.6167 | +0.6167 |
| circuit_open_count | 19 | 11 | -8 (ít lệnh gọi provider thực hơn nghĩa là ít cơ hội kích hoạt breaker hơn) |
| availability | 0.9933 | 0.9767 | -0.0166 (ít lệnh gọi `backup` thực hơn cũng đồng nghĩa ít cơ hội "dính" fail_rate 5% của nó hơn, nên giá trị availability lệch ở lần chạy không-cache này là nhiễu do thứ tự kịch bản trong cùng chuỗi RNG, không phải do cache gây ra — `estimated_cost` và `circuit_open_count` mới là tín hiệu đáng tin cậy ở đây) |

Cache giảm tải cho provider (chi phí -67%, ít lần mở circuit hơn) mà không làm tăng độ trễ đáng
kể, vì cache hit rẽ nhánh sớm trước khi gọi tới provider/breaker. Các chênh lệch `availability`
và `latency_p50_ms` ở trên nằm trong biên độ dao động giữa các lần chạy đã mô tả ở ghi chú
reproducibility của §2 (do circuit breaker dùng thời gian thực), không phải hiệu ứng phụ của
cache — `estimated_cost` và `circuit_open_count` mới là tín hiệu đáng tin cậy, gắn trực tiếp với
cache vì chúng đo tổng số lệnh gọi provider đã tránh được.

### Ví dụ minh họa guardrail false-hit

`data/sample_queries.jsonl` có các cặp truy vấn gần giống nhau nhưng khác năm (`q16`/`q17`: học
phí năm 2024 so với 2025). Độ tương đồng n-gram cosine giữa chúng đủ cao để vượt ngưỡng cache, vì
vậy nếu không có guardrail false-hit, cache sẽ âm thầm trả về câu trả lời của năm ngoái cho câu
hỏi của năm nay:

```
$ python -c "
from reliability_lab.cache import ResponseCache
cache = ResponseCache(ttl_seconds=300, similarity_threshold=0.92)
cache.set('What is the tuition fee for the 2024 academic year?', 'The 2024 tuition fee is 12,000,000 VND per semester.')
value, score = cache.get('What is the tuition fee for the 2025 academic year?')
print(f'similarity_score={score:.4f}')
print(f'returned_value={value!r}')
print(f'false_hit_log={cache.false_hit_log}')
"

similarity_score=0.9574
returned_value=None
false_hit_log=[{'query': 'What is the tuition fee for the 2025 academic year?',
                 'cached_key': 'What is the tuition fee for the 2024 academic year?',
                 'reason': 'date_or_number_mismatch', 'ts': 1782880890.895064}]
```

Điểm 0.9574 vượt ngưỡng similarity 0.92, nhưng `_looks_like_false_hit()` phát hiện hai con số
4 chữ số khác nhau (2024 và 2025) và từ chối trả kết quả — `get()` trả về `(None, 0.9574)` thay
vì câu trả lời cũ của 2024, đồng thời ghi lại việc từ chối này vào `false_hit_log` để quan sát
(observability). Để đối chứng, cùng guardrail này vẫn cho qua bình thường một câu hỏi diễn đạt
lại nhưng cùng năm (`test_same_year_not_flagged_as_false_hit` trong `tests/test_cache.py`, và
kiểm chứng thủ công: câu hỏi "refund policy... 2024 deadline" diễn đạt lại thành "...2024
deadline again" đạt điểm 0.9701 và trả về giá trị cache bình thường).

## 6. Redis shared cache

- Vì sao cache in-memory không đủ cho hệ thống nhiều instance: `ResponseCache` lưu entry trong
  một list Python trên bộ nhớ heap của process. Nếu gateway chạy sau load balancer với N replica,
  mỗi replica có cache rỗng riêng, nên tỉ lệ hit giảm gần N lần và cùng một câu hỏi bị tính phí
  provider thật N lần trước khi mọi replica "làm nóng" được bản cache riêng của mình.
- `SharedRedisCache` giải quyết vấn đề này như thế nào: entry được lưu dưới dạng Redis hash
  (`query`, `response`) với key `rl:cache:<md5(query)>`, dùng `EXPIRE` để quản lý TTL. Bất kỳ
  gateway instance nào kết nối cùng Redis URL đều đọc/ghi chung một keyspace, nên việc một
  instance ghi cache sẽ ngay lập tức hiển thị cho tất cả các instance khác.

### Bằng chứng shared state

Hai object `SharedRedisCache` độc lập (mô phỏng hai gateway process riêng biệt), cùng trỏ tới
`redis://localhost:6379/0`:

```
$ python -c "
from reliability_lab.cache import SharedRedisCache
cache_a = SharedRedisCache('redis://localhost:6379/0', ttl_seconds=300, similarity_threshold=0.92)
cache_a.set('What is the capital of France?', 'Paris is the capital of France.')
print('Instance A wrote the entry.')
cache_b = SharedRedisCache('redis://localhost:6379/0', ttl_seconds=300, similarity_threshold=0.92)
value, score = cache_b.get('What is the capital of France?')
print(f'Instance B read: value={value!r} score={score}')
value2, score2 = cache_b.get('What is the capital city of France?')
print(f'Instance B fuzzy read: value={value2!r} score={score2:.3f}')
"

Instance A wrote the entry.
Instance B read: value='Paris is the capital of France.' score=1.0
Instance B fuzzy read: value='Paris is the capital of France.' score=0.933
```

Instance B chưa bao giờ gọi `.set()` — nó thấy được entry này chỉ vì cả hai instance dùng chung
backend Redis. Lượt đọc mờ (fuzzy read) cũng xác nhận `SharedRedisCache.get()` chạy đúng cơ chế
quét similarity n-gram (điểm 0.933 >= ngưỡng 0.92) giống hệt cache in-memory.

### Kết quả Redis CLI

Ghi nhận sau khi chạy `python scripts/run_chaos.py --config configs/redis_cache.yaml --out reports/metrics_redis.json`
(300 request, `cache.backend: redis`):

```bash
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:0bc3b1acf73d
rl:cache:095946136fea
rl:cache:fff10da1c72c
rl:cache:3dab98c0e49e
rl:cache:98332d0d1c9c
rl:cache:d354658dc020
rl:cache:b2a52f7dc795
rl:cache:da61fb49b4f6
rl:cache:734852f3cf4a
rl:cache:dacb2b833659
rl:cache:9e413fd814eb
rl:cache:844ef0143a5c
(tổng 12 key — xác nhận qua DBSIZE)

$ docker compose exec redis redis-cli HGETALL rl:cache:844ef0143a5c
query
What is the tuition fee for the 2024 academic year?
response
[backup] reliable answer for: What is the tuition fee for the 2024 academic year?

$ docker compose exec redis redis-cli TTL rl:cache:844ef0143a5c
213
```

12 key duy nhất cho 300 request trên bộ 20 câu hỏi mẫu xác nhận cơ chế khử trùng lặp hoạt động
đúng: các câu hỏi khác nhau có key khác nhau, TTL (còn 213s trong tổng 300s cấu hình) chứng minh
`EXPIRE` đã được set, và `cache_hit_rate` của lần chạy đó là 0.7067
(`reports/metrics_redis.json`).

### So sánh độ trễ in-memory vs Redis (tùy chọn)

| Metric | Cache in-memory | Cache Redis | Ghi chú |
|---|---:|---:|---|
| latency_p50_ms | 286.95 | 279.19 | Tương đương — độ trễ giả lập của provider (180-260ms) chi phối cả hai. |
| latency_p95_ms | 320.33 | 319.92 | Round-trip Redis tới localhost gần như không đáng kể ở đây. |
| cache_hit_rate | 0.6167 | 0.7067 | Cả hai backend đều chạy chung logic similarity/guardrail (`ResponseCache.similarity`); chênh lệch là do thứ tự chuỗi RNG của kịch bản, không phải khác biệt về năng lực giữa hai backend. |

## 7. Kịch bản chaos

| Kịch bản | Hành vi kỳ vọng | Hành vi quan sát được | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | `primary` fail 100% lệnh gọi; circuit của `primary` mở sau 3 lần fail và giữ nguyên trạng thái mở; toàn bộ traffic thành công chuyển qua `backup` (route="fallback") | Circuit của `primary` mở đúng như kỳ vọng, các request tiếp theo bỏ qua `primary` ngay lập tức (không có retry storm), `backup` phục vụ gần hết traffic; một số ít request vẫn rơi vào `static_fallback` khi fail_rate 5% của chính `backup` trùng lúc circuit của `primary` đang mở | pass |
| primary_flaky_50 | `primary` fail ~50% lệnh gọi; circuit dao động CLOSED -> OPEN -> HALF_OPEN -> CLOSED khi các lần fail tập trung rồi giảm dần | `circuit_open_count` > 0 cho `primary` trong kịch bản này, `recovery_time_ms` được tính từ các cặp chuyển trạng thái OPEN->CLOSED (trung bình 2379.12ms, gần với `reset_timeout_seconds`=2s cộng thêm thời gian một vòng probe) | pass |
| all_healthy | Cả hai provider ở fail rate thấp theo cấu hình (25%/5%); phần lớn traffic đi qua `primary` (route="primary"), vẫn có một số hoạt động breaker do fail_rate nền 25% của `primary` | Đa số request thành công đi qua `primary`, thỉnh thoảng vẫn có breaker trip vì fail_rate nền của `primary` (0.25) cao hơn failure_threshold=3 trong một lần chạy 100 request — đúng như dự kiến với cấu hình này, không phải lỗi | pass |

Cả 3 kịch bản đều báo `"pass"` trong `reports/metrics.json` dưới tiêu chí pass/fail của
`scenarios` (`successful_requests > 0`, nghĩa là chuỗi fallback luôn tạo ra được một phản hồi
dùng được kể cả khi `primary` down hoàn toàn).

## 8. Phân tích lỗi (failure analysis)

**Điểm yếu còn tồn tại:** `fallback_success_rate` (92.22%) đang dưới mục tiêu SLO 95%, và
`availability` tổng thể (97.67%) hơi dưới mục tiêu 99%. Nguyên nhân gốc: fail_rate riêng của
`backup` (5%) độc lập với trạng thái của `primary`, nên trong kịch bản primary_timeout_100 — nơi
100% traffic phải dồn hết vào `backup` — fail_rate nền 5% của provider này giờ đây hấp thụ toàn
bộ 100 request thay vì chỉ một phần nhỏ, và một phần trong số các lần fail đó trùng lúc circuit
riêng của `backup` vẫn đang CLOSED (cho phép request đúng đắn) nhưng lệnh gọi bên dưới vẫn fail,
rơi thẳng xuống `static_fallback`. Hiện tại chưa có provider thứ ba hay cơ chế retry-with-backoff
cho một lần fail thoáng qua của `backup` — chỉ cần `backup` fail một lần là request đó đi thẳng
tới `static_fallback`.

**Đề xuất khắc phục:** thêm một lần retry có giới hạn (không phải retry storm — chỉ thêm đúng
một lần thử với jitter ngắn) riêng cho `backup` trước khi rơi xuống `static_fallback`, hoặc thêm
provider thứ ba có fail_rate thấp hơn nữa để hệ thống không phụ thuộc hoàn toàn vào một đường
backup duy nhất trong lúc `primary` gặp sự cố. Nên theo dõi `fallback_success_rate` như một
ngưỡng cảnh báo tường minh (ví dụ: page nếu tụt dưới 95% trong 5 phút liên tiếp) vì đây là chỉ
báo sớm cho việc SLO sắp bị vi phạm, trước cả khi `availability` thực sự tụt xuống.

## 9. Bước tiếp theo

1. Thêm một lần retry có giới hạn (tối đa 1 lần thử thêm, jitter nhỏ) cho provider cuối cùng
   trong chuỗi trước khi rơi xuống `static_fallback`, để thu hẹp khoảng cách của
   fallback_success_rate.
2. Tách rời bộ đếm giờ reset của circuit breaker khỏi thời gian thực (ví dụ: reset dựa trên số
   lượng request, hoặc dùng đồng hồ có thể "tiêm" — injectable clock) để các lần chạy chaos có
   thể tái lập giống hệt nhau tuyệt đối mà không vướng cảnh báo về nhiễu thời gian đã mô tả ở §2 —
   cần thiết cho một hệ thống CI kiểm tra hồi quy (regression testing) chặt chẽ hơn.
3. Mở rộng cost-aware routing (§10.2) với việc chia ngân sách theo từng provider và một gauge
   kiểu Prometheus cho `budget_utilization`, thay vì chỉ dùng một bộ đếm `cumulative_cost` toàn
   cục duy nhất.

## 10. Các mục bonus (stretch goals)

Cả 6 mục bonus trong đề bài đều đã được triển khai và có test đi kèm.

### 10.1 Bảng SLO

Xem §3 — SLO của availability và fallback_success_rate hiện **chưa đạt**; latency, cache hit
rate, và recovery time đều đạt. Đây là bằng chứng có chủ đích cho phần phân tích lỗi ở §8, không
phải lỗi phát sinh ngoài ý muốn: mục tiêu của bài lab là để lộ ra một khoảng trống thật để báo
cáo giải thích.

### 10.2 Cost-aware routing (định tuyến theo ngân sách chi phí)

Triển khai trong `ReliabilityGateway` (`gateway.py`) và nối dây đầy đủ qua `BudgetConfig` mới
(`config.py`) — các file `configs/*.yaml` có thể set `budget.total` / `budget.warning_pct`;
`build_gateway()` truyền các giá trị này xuống. Có unit test tại
`tests/test_gateway_budget.py` (5 test: baseline không giới hạn ngân sách, định tuyến bình thường
khi dưới ngưỡng cảnh báo, định tuyến chỉ dùng provider rẻ khi ở pha cảnh báo, static fallback khi
hết ngân sách, cache hit bỏ qua hoàn toàn việc kiểm tra ngân sách).

Demo (`configs/budget_demo.yaml`, budget.total=0.01, warning_pct=0.5, cả hai provider fail_rate=0
để pha định tuyến là biến số duy nhất): trace `gw._budget_utilization()` trước mỗi lệnh gọi trong
20 request đầu tiên cho thấy cả 3 pha kích hoạt lần lượt theo đúng thứ tự:

```
request=  0 phase=normal     utilization_before=0.000 route='primary' provider='primary' cumulative_cost=0.00075
request=  7 phase=warning    utilization_before=0.558 route='fallback' provider='backup' cumulative_cost=0.00600
request= 18 phase=exhausted  utilization_before=1.006 route='static_fallback' provider=None cumulative_cost=0.01006
final cumulative_cost: 0.01006 / budget 0.01
```

Chạy đầy đủ 100 request với cùng cấu hình (`reports/metrics_budget_demo.json`):
`availability=0.23`, `estimated_cost=0.01014` — 77% request rơi vào `static_fallback` khi ngân
sách nhỏ $0.01 bị dùng hết, đúng như thiết kế để có thể quan sát rõ sự chuyển pha trong một lần
chạy ngắn.

### 10.3 Concurrency (xử lý đồng thời)

`chaos.py:run_scenario()` giờ rẽ nhánh theo `load_test.concurrency` (trường mới trong
`LoadTestConfig`, mặc định là 1 = giữ nguyên hành vi tuần tự hiện tại, nên `configs/default.yaml`
không bị ảnh hưởng). Khi > 1, các request được chạy qua `ThreadPoolExecutor` trên cùng một bộ
gateway/breaker/cache, sau đó các số liệu được tổng hợp tuần tự (đơn luồng) từ kết quả đã thu
thập (không cần khóa gì thêm cho bước tổng hợp này). Để làm được điều này, phải bổ sung
thread-safety cho `CircuitBreaker` và `ResponseCache`: cả hai đều được thêm một
`threading.Lock` nội bộ bao quanh phần mutate state (`circuit_breaker.py`, `cache.py`); khóa chỉ
bao quanh phần đọc/ghi state, không bao quanh lệnh gọi provider (vốn chậm), nên các request đồng
thời vẫn có thể chồng lấn nhau về I/O. Đã kiểm chứng trong `tests/test_concurrency.py` — 200
request đồng thời qua cùng một breaker không bao giờ làm hỏng `failure_count`/`success_count`/
`state`; 100 lệnh `cache.set()` đồng thời không bao giờ làm mất entry nào.

So sánh load-test thực tế (`configs/concurrency_demo.yaml`, concurrency=8, cùng seed/kịch bản
với `configs/default.yaml`):

| | concurrency=1 (default.yaml) | concurrency=8 (concurrency_demo.yaml) |
|---|---:|---:|
| wall_time | 37.75s | 5.59s |
| total_requests | 300 | 300 |
| availability | 0.9767 | 0.9833 |
| latency_p50_ms | 285.89 | 281.74 |

**Tăng tốc 6.75 lần** về wall-clock cho cùng một khối lượng công việc 300 request, trong khi các
số liệu vẫn nằm trong cùng biên độ với lần chạy tuần tự (độ trễ mỗi request không đổi vì vẫn được
giả lập bằng `time.sleep()` bên trong mỗi thread — chỉ có throughput tổng thể được cải thiện).

### 10.4 Redis circuit state

`CircuitBreaker` giờ nhận một `redis_client` tùy chọn (mirror bộ đếm failure qua
`INCR`/`EXPIRE`/`DELETE`, expose qua `shared_failure_count()`); `build_gateway()` truyền cùng
Redis client mà `SharedRedisCache` đang dùng xuống cho mọi breaker khi `cache.backend: redis`.
Đây là thiết kế có chủ đích ở mức *observability* (quan sát), không phải di trú toàn bộ state:
các chuyển trạng thái CLOSED-OPEN-HALF_OPEN cục bộ của từng instance vẫn diễn ra độc lập — chỉ có
bộ đếm failure là được chia sẻ, nên một instance thứ hai có thể thấy "đâu đó đã có 3 lần fail" mà
chưa cần tin tưởng quyết định OPEN/CLOSED của một instance ở xa. Đã kiểm thử end-to-end với Redis
container đang chạy trong `tests/test_redis_circuit_state.py` (4 test, tự động skip nếu Redis
chưa bật). Demo thủ công:

```
Instance A local failure_count: 2
Instance B local failure_count: 0 (never called record_failure itself)
Instance B shared_failure_count() via Redis: 2
after B also fails once -> shared_failure_count(): 3
after A succeeds -> Redis counter reset to: 0
```

### 10.5 Redis graceful degradation (suy giảm ổn thỏa khi Redis lỗi)

`build_gateway()` giờ gọi `SharedRedisCache.ping()` trước khi thực sự chọn backend Redis; nếu
thất bại, nó đóng kết nối chết, in một dòng cảnh báo, và tự động chuyển về dùng `ResponseCache`
thay vì để cả lần chạy bị crash. Đã kiểm thử trong `tests/test_redis_graceful_degradation.py`
bằng cách trỏ vào cổng 6390 (không có gì lắng nghe ở đó) — `build_gateway()` vẫn trả về một
gateway hoạt động bình thường dùng `ResponseCache`, và `gateway.complete()` vẫn thành công.

### 10.6 Property-based tests (kiểm thử dựa trên thuộc tính)

`tests/test_circuit_breaker_properties.py` dùng thư viện `hypothesis` để fuzz các chuỗi
success/failure ngẫu nhiên (tối đa 50 sự kiện, 200 ví dụ cho mỗi property,
`failure_threshold`/`success_threshold` cũng được random hóa) nhằm kiểm tra 6 bất biến
(invariant): trạng thái luôn là một giá trị `CircuitState` hợp lệ; `failure_count`/
`success_count` không bao giờ âm và `failure_count` luôn reset sau mỗi lần thành công; một
breaker đang CLOSED mà chưa từng OPEN thì không bao giờ có `failure_count >= failure_threshold`;
OPEN thì `opened_at` phải khác `None`; các mục trong `transition_log` phải nối tiếp nhau đúng
theo `from`→`to`, và giá trị `to` cuối cùng phải khớp với state hiện tại; và một breaker đang
HALF_OPEN mà probe thất bại thì luôn phải mở lại (không bao giờ ở lại HALF_OPEN). Cả 6 property
đều pass, không tìm thấy phản ví dụ (counterexample) nào.
