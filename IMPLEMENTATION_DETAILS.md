# 3D CTS Implementation Details (Slides 8-10 Reference)

## Overview: What We Built

3D CTS = **Phase 1 (CTS)** + **Phase 2 (Useful Skew)** + **Phase 3 (Buffer Sizing)**

```
cts_3d.tcl (entry point)
  │
  ├── Phase 1: clock_tree_synthesis (TritonCTS C++)
  │     └── HTreeBuilder: getDominantTier() → mapBufferMasterToTier()
  │
  ├── Phase 2: useful_skew.tcl
  │     ├── extract_ff_graph (VerilogFFExtractor C++) → ff_timing_graph.csv
  │     ├── useful_skew_lp.py (scipy LP solver with ΔHB)
  │     └── insert_delay_buffer_odb (direct ODB API)
  │
  └── Phase 3: buffer_sizing_iterative.tcl
        ├── parse FFGraph → find worst-slack FFs
        ├── swapMaster() to upsize buffers
        └── estimate_parasitics → verify TNS → accept/rollback
```

---

## Part 1: OpenROAD C++ Modifications (5 files)

### 1-1. HTreeBuilder.cpp — Tier-Aware Buffer Selection

**What it does**: CTS가 clock tree를 만들 때, 각 클러스터(FF 그룹)가 어느 tier에 있는지 보고, 해당 tier의 버퍼를 선택함.

**기존 (Pin3D)**: 버퍼를 항상 하나만 사용 (예: `CLKBUF_X3_bottom`)
**우리가 바꾼 것**: 클러스터 내 FF 다수가 upper tier면 `CLKBUF_X3_upper`, bottom이면 `CLKBUF_X3_bottom` 사용

#### 추가된 함수 3개:

**`getDominantTierFromInsts()`** (HTreeBuilder.cpp L350-374)
```cpp
// 클러스터 내 인스턴스들의 tier를 카운팅해서 다수 tier 반환
int HTreeBuilder::getDominantTierFromInsts(const vector<ClockInst*>& insts) {
    int tier0_count = 0, tier1_count = 0;
    for (auto* inst : insts) {
        odb::dbInst* dbInst = ...;    // ClockInst → ODB 인스턴스 변환
        int tier = dbInst->getTier(); // ODB API로 tier 조회
        if (tier == 0) tier0_count++;
        else if (tier == 1) tier1_count++;
    }
    return (tier1_count > tier0_count) ? 1 : 0;  // 다수 tier 반환
}
```

**`getDominantTierFromSinkLocs()`** (L380-411) — 좌표 기반 동일 로직 (branch buffer tier 결정에 사용)
**`getDominantTierFromClockSinks()`** (L413-430) — 전체 clock sink 기반 (root buffer tier 결정에 사용)

#### Bug Fix #1: Sink ClockInst의 instObj_ 미설정 (Clock.h)

**문제**: `Clock::addSink()`에서 `sinks_.emplace_back()` 후 `setInstObj()`를 호출하지 않아서, 모든 sink ClockInst의 `getDbInst()`가 null 반환. `getDominantTierFromInsts()`가 항상 tier=-1 반환 → 모든 leaf buffer가 bottom tier로 배치됨.

**수정** (Clock.h L211-213, L224-226):
```cpp
void addSink(..., odb::dbITerm* pinObj, float inputCap) {
    sinks_.emplace_back(name, "", CLOCK_SINK, x, y, pinObj, inputCap);
    if (pinObj) {
        sinks_.back().setInstObj(pinObj->getInst());  // ← 추가
    }
}
```

#### Bug Fix #2: Branch buffer tier=-1 — ClockInst에 tier_ 필드 추가 (Clock.h, HTreeBuilder.cpp)

**문제**: H-tree는 bottom-up (leaf → branch → root) 순서로 빌드됨. Leaf clustering 후 `mapLocationToSink_`가 leaf buffer ClockInst로 갱신되는데, 이 시점에서 leaf buffer의 dbInst(ODB)는 아직 생성되지 않음 (토폴로지 완성 후 `createClockBuffers()`에서 일괄 생성). 따라서 branch buffer의 tier 결정 시 `getDominantTierFromSinkLocs()` → `getDbInst()` → null → tier=-1.

**수정 1** — ClockInst에 `tier_` 필드 추가 (Clock.h L75-76, L89):
```cpp
class ClockInst {
 public:
    void setTier(int tier) { tier_ = tier; }
    int getTier() const { return tier_; }
 private:
    int tier_ = -1;  // tier index for 3D CTS (-1 = unknown)
};
```

**수정 2** — Leaf buffer 생성 시 tier 저장 (HTreeBuilder.cpp L266):
```cpp
ClockInst& rootBuffer = clock_.addClockBuffer(...);
rootBuffer.setTier(target_tier);  // ← 추가: 계산된 tier를 ClockInst에 저장
```

**수정 3** — `getDominantTierFromSinkLocs()` fallback (HTreeBuilder.cpp L394-400):
```cpp
int tier = -1;
odb::dbInst* db_inst = inst->getDbInst();
if (db_inst != nullptr) {
    tier = db_inst->getTier();    // 정상 경로 (FF sink)
} else {
    tier = inst->getTier();       // fallback (leaf buffer, dbInst 미생성)
}
```

**결과**: Branch L1/L2/L3 모두 leaf buffer의 tier 정보를 통해 올바른 tier-aware buffer 선택 가능.

#### 추가된 Helper 함수:

**`mapBufferMasterToTier()`** (HTreeBuilder.cpp L37-72, anonymous namespace)
```cpp
// 버퍼 이름을 tier에 맞게 변환
// 예: "CLKBUF_X3" + tier=1 → "CLKBUF_X3_upper"
// 예: "CLKBUF_X3" + tier=0 → "CLKBUF_X3_bottom"
string mapBufferMasterToTier(const string& master, int target_tier, odb::dbDatabase* db) {
    string suffix = (target_tier == 1) ? "_upper" : "_bottom";
    string tiered_name = master_base + suffix;

    // DB에 해당 이름의 셀이 실제로 있는지 확인
    for (auto* lib : db->getLibs()) {
        if (lib->findMaster(tiered_name.c_str())) {
            return tiered_name;  // 있으면 tier-specific 버퍼 사용
        }
    }
    return master;  // 없으면 원래 이름 유지
}
```

#### 호출 지점 (~10곳):
| 위치 | 용도 |
|------|------|
| L251 | 클러스터별 sink buffer 배정 |
| L2061 | root buffer tier 결정 |
| L2103 | first-level branch buffer |
| L2137 | tree buffer |
| L2169 | second-level branch buffer |
| L2211 | nested tree buffer |
| L2440 | SegmentBuilder segment buffer |
| L2488 | forceBufferInSegment |

#### SegmentBuilder 수정 (L2383-2407):
```cpp
// SegmentBuilder 생성자에 targetTier_ 파라미터 추가
SegmentBuilder::SegmentBuilder(..., int targetTier)
    : ..., targetTier_(targetTier) {}
// → 세그먼트(wire segment) 중간에 삽입하는 버퍼도 tier-specific으로 선택
```

---

### 1-2. VerilogFFExtractor.cpp — FF Timing Graph 추출

**What it does**: Verilog 넷리스트를 파싱해서 FF-to-FF 연결 관계 + 위치(XY, tier) + 타이밍(slack) 정보를 CSV로 출력. LP solver의 입력 데이터.

**이 모듈은 완전 신규 개발 (Pin3D에 없었음)**

#### 전체 흐름:
```
1. parseVerilog()     — Verilog 텍스트에서 FF 인스턴스 + 연결 관계 추출 (regex)
2. extractFFEdges()   — FF → combinational logic → FF 경로 추출 (BFS)
3. fillLocations()    — ODB에서 각 FF의 XY 좌표 + tier 추출
4. fillTimingInfo()   — OpenSTA에서 setup/hold slack 추출
5. writeCSV()         — ff_timing_graph.csv 출력
```

#### FFEdgeVerilog 구조체 (VerilogFFExtractor.h L29-51):
```cpp
struct FFEdgeVerilog {
    string from_ff;         // source FF 이름
    string to_ff;           // destination FF 이름
    double slack_max;       // setup slack (ns)
    double slack_min;       // hold slack (ns)
    int from_x, from_y;    // source FF 좌표
    int from_tier;          // source FF tier (0=bottom, 1=upper)  ← 우리가 추가
    int to_x, to_y;        // dest FF 좌표
    int to_tier;            // dest FF tier (0=bottom, 1=upper)    ← 우리가 추가
};
```

#### Tier 결정 방법 (fillLocations, L255-287):
```cpp
void VerilogFFExtractor::fillLocations(FFEdgeVerilog& edge) {
    odb::dbInst* inst = block_->findInst(edge.from_ff.c_str());
    string master = inst->getMaster()->getName();

    // master name suffix로 tier 판별
    // 예: "DFFHQNx1_ASAP7_75t_R_upper" → tier 1
    // 예: "DFFHQNx1_ASAP7_75t_R_bottom" → tier 0
    edge.from_tier = (master.find("_upper") != string::npos) ? 1 : 0;
}
```

#### CSV 출력 형식 (writeCSV, L463-488):
```
from_ff,to_ff,slack_max_ns,slack_min_ns,from_x,from_y,from_tier,to_x,to_y,to_tier
_16693__upper,_16463__upper,-0.162,0.050,1200,3400,1,1500,3200,1
_16441__bottom,_16418__upper,-0.157,0.030,800,2100,0,1300,3100,1
```
→ `from_tier`과 `to_tier`이 다르면 **cross-tier path** → LP에서 ΔHB 페널티 적용

#### STA 타이밍 추출 (fillTimingInfo, L289-462):
```cpp
void VerilogFFExtractor::fillTimingInfo(vector<FFEdgeVerilog>& edges) {
    sta_->ensureGraph();
    sta_->ensureClkArrivals();

    // 각 edge에 대해 STA API로 slack 추출
    // findPathEnds() → setup slack, hold slack
    for (auto& edge : edges) {
        sta::Pin* from_pin = network_->findPin(from_inst, "CLK");
        auto* path_ends = sta_->findPathEnds(from_pin, ...);
        edge.slack_max = path_end->slack(sta::MinMax::max());  // setup
        edge.slack_min = path_end->slack(sta::MinMax::min());  // hold
    }
}
```

#### TCL 진입점 (TritonCTS.cpp L2600-2628):
```cpp
// TCL에서 "extract_ff_graph 3_place.v ff_timing_graph.csv" 호출 시 실행
void TritonCTS::extractFFGraphFromVerilog(const string& verilog, const string& output) {
    VerilogFFExtractor extractor(verilog, block_, sta_, network_, logger_);
    extractor.parseVerilog();
    auto edges = extractor.extractFFEdges();
    extractor.fillTimingInfo(edges);
    extractor.writeCSV(edges, output);
}
```

---

### 1-3. CtsOptions.h — 변경 없음

`enable3dCts`, `upperTierBuffer` 같은 옵션은 **추가하지 않았음**.
대신 tier 정보는 ODB의 `getTier()` API와 master name suffix convention으로 처리.

### 1-4. SinkClustering.h/cpp — 변경 없음

클러스터링 알고리즘 자체는 수정하지 않음. Tier-aware 처리는 HTreeBuilder 단에서 수행.

---

## Part 2: Flow-3DCTS TCL/Python Scripts

### 2-1. cts_3d.tcl — 메인 진입점

**위치**: `scripts_openroad/cts_3d.tcl`

```tcl
# Line 10: 디자인 로드
load_design 3_place.v 3_place.sdc "Starting 3D CTS..."

# Line 12-13: 유틸리티 로드
source $::env(OPENROAD_SCRIPTS_DIR)/placement_utils.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/floorplan_utils.tcl

# Tier 분류 (master name → ODB setTier)
set_tier_from_master_names

# ===== Phase 1: Standard CTS =====
# 양쪽 tier 버퍼를 CTS_BUF_LIST에 동시 제공
# → HTreeBuilder가 getDominantTier()로 자동 선택
clock_tree_synthesis -buf_list "$CTS_BUF_BOTTOM $CTS_BUF_UPPER" \
                     -root_buf $CTS_ROOT_BUF \
                     -sink_clustering_size $CTS_CLUSTER_SIZE

detailed_placement
estimate_parasitics -placement

# 타이밍 보고: "Before Optimization"
report_tns; report_wns

# ===== Phase 2: Useful Skew (조건부) =====
if { $::env(ENABLE_BUFFER_INSERTION) == 1 } {
    source $::env(OPENROAD_SCRIPTS_DIR)/useful_skew.tcl
    run_useful_skew ...
    # 타이밍 보고: "After Useful Skew"
}

# ===== Phase 3: Buffer Sizing (조건부) =====
if { $::env(ENABLE_BUFFER_SIZING) == 1 } {
    source $::env(OPENROAD_SCRIPTS_DIR)/buffer_sizing_iterative.tcl
    run_iterative_buffer_sizing ...
    # 타이밍 보고: "After Buffer Sizing"
}

# Cross-tier 통계 보고
count_cross_tier_nets
report_cross_tier_stats
```

#### `count_cross_tier_nets` proc (L181-227):
```tcl
# clock net에 연결된 인스턴스들의 tier를 확인
# 같은 net에 tier 0과 tier 1 인스턴스가 섞여있으면 cross-tier
proc count_cross_tier_nets {} {
    foreach net [get_clock_nets] {
        set tiers {}
        foreach inst [$net getInstTerms] {
            lappend tiers [[$inst getInst] getTier]
        }
        if {[lsort -unique $tiers] > 1} {
            incr cross_tier_count
        }
    }
    puts "Cross-tier clock nets: $cross_tier_count / $total_nets"
}
```

---

### 2-2. useful_skew.tcl — LP 기반 버퍼 삽입 (Phase 2)

**위치**: `scripts_openroad/useful_skew.tcl`

#### 핵심 proc: `insert_delay_buffer_odb` (L14-134)

**이것이 Workaround**: OpenROAD의 `insert_buffer` 명령이 3D 디자인에서 ODB crash를 일으키므로,
직접 ODB C API를 TCL에서 호출하여 버퍼를 생성함.

```tcl
proc insert_delay_buffer_odb {inst_name pin_name buf_master_name} {
    set block [ord::get_db_block]
    set inst  [$block findInst $inst_name]
    set pin   [$inst findITerm $pin_name]          ;# 예: CLK 핀
    set old_net [$pin getNet]                       ;# FF의 기존 clock net

    # 1. 새 net 생성 (FF와 버퍼 사이)
    set new_net [odb::dbNet_create $block "skew_net_${inst_name}"]

    # 2. 버퍼 인스턴스 생성
    set buf_master [$db findMaster $buf_master_name]
    set buf [odb::dbInst_create $block $buf_master "skew_buf_${inst_name}"]

    # 3. 버퍼를 FF 옆에 배치
    set origin [$inst getOrigin]
    $buf setOrigin [lindex $origin 0] [lindex $origin 1]
    $buf setPlacementStatus PLACED

    # 4. 버퍼의 tier를 FF와 같게 설정
    set ff_tier [$inst getTier]
    $buf setTier $ff_tier

    # 5. 네트 연결 변경
    #    기존: clock_net → FF/CLK
    #    변경: clock_net → BUF/A → new_net → FF/CLK
    $pin disconnect
    $pin connect $new_net
    # 버퍼 입력을 old_net에 연결
    [[$buf findITerm "A"] connect $old_net]
    # 버퍼 출력을 new_net에 연결
    [[$buf findITerm "Y"] connect $new_net]

    return [list $buf $old_net $new_net]
}
```

**그림으로 보면:**
```
Before:  clock_net ──────────────────── FF/CLK
After:   clock_net ── BUF/A → BUF/Y ── new_net ── FF/CLK
                      (delay buffer)
         BUF의 delay만큼 clock 도착이 늦어짐 → FF의 setup slack 개선
```

#### 메인 흐름: `run_useful_skew` (L232-439)

```tcl
proc run_useful_skew {max_ffs max_bufs buf_delay_ns batch_size} {
    # Step 1: FF timing graph 추출 (C++ 호출)
    extract_ff_graph $verilog_file $ff_graph_csv

    # Step 2: LP solver 호출 (Python)
    exec python3 useful_skew_lp.py $ff_graph_csv $lp_result_csv \
         --max_skew $max_skew --buf_delay $buf_delay_ns

    # Step 3: LP 결과 파싱 → 버퍼 삽입 대상 FF 목록
    set ff_list [parse_lp_result $lp_result_csv]
    # LP가 "이 FF에 버퍼 N개 삽입하라"고 알려줌

    # Step 4: 배치(batch) 단위로 버퍼 삽입 + 검증
    set inserted_bufs {}
    set prev_tns [get_tns]

    foreach ff $ff_list {
        # 버퍼 master 선택 (FF tier에 맞춤)
        set tier [get_ff_tier $ff]
        if {$tier == 1} {
            set buf_master "BUFx2_ASAP7_75t_R_upper"
        } else {
            set buf_master "BUFx2_ASAP7_75t_R_bottom"
        }

        # 버퍼 삽입
        set result [insert_delay_buffer_odb $ff CLK $buf_master]
        lappend inserted_bufs $result

        # batch_size마다 검증
        if {[llength $inserted_bufs] % $batch_size == 0} {
            detailed_placement
            estimate_parasitics -placement
            set new_tns [get_tns]

            if {$new_tns > $prev_tns} {
                # TNS 악화 → 마지막 batch 롤백
                foreach buf [last_batch] { remove_buffer $buf }
            } else {
                set prev_tns $new_tns
            }
        }
    }
}
```

---

### 2-3. useful_skew_lp.py — LP Solver (Phase 2의 두뇌)

**위치**: `scripts_openroad/useful_skew_lp.py`

#### LP 문제 정의:

```
maximize  Σ s_i        (총 삽입 skew 최대화)

subject to:
  Setup:  s_i - s_j ≤ slack_setup(i,j) - ΔHB(i,j)     ← 핵심: ΔHB 항
  Hold:   s_j - s_i ≤ slack_hold(i,j) - ΔHB(i,j)
  Bounds: 0 ≤ s_i ≤ s_max

여기서 ΔHB(i,j) = δ_HB  if tier(i) ≠ tier(j)   (cross-tier penalty ~20ps)
                 = 0     if tier(i) == tier(j)     (same-tier)
```

#### 코드 핵심 부분:

```python
def solve_useful_skew_lp(ff_graph_csv, max_skew_ns, buf_delay_ns, hb_delay_ns=0.020):
    # 1. FF timing graph CSV 파싱
    edges = parse_ff_timing_graph(ff_graph_csv)
    # edges = [(from_ff, to_ff, slack_setup, slack_hold, from_tier, to_tier), ...]

    ff_names = list(set([e.from_ff for e in edges] + [e.to_ff for e in edges]))
    n = len(ff_names)  # FF 개수 (예: AES = 535개)

    # 2. LP 구성
    # 목적함수: maximize Σ s_i  →  minimize -Σ s_i
    c = [-1.0] * n  # scipy.linprog는 minimize이므로 부호 반전

    A_ub = []  # 부등식 제약 행렬
    b_ub = []  # 부등식 제약 우변

    for (i, j, slack_setup, slack_hold, ti, tj) in edges:
        # ΔHB: cross-tier이면 20ps penalty
        delta_hb = hb_delay_ns if (ti != tj) else 0.0

        # Setup constraint: s_i - s_j ≤ slack_setup - ΔHB
        row = [0.0] * n
        row[idx_i] = 1.0
        row[idx_j] = -1.0
        A_ub.append(row)
        b_ub.append(slack_setup - delta_hb)

        # Hold constraint: s_j - s_i ≤ slack_hold - ΔHB
        row = [0.0] * n
        row[idx_j] = 1.0
        row[idx_i] = -1.0
        A_ub.append(row)
        b_ub.append(slack_hold - delta_hb)

    # Bounds: 0 ≤ s_i ≤ max_skew
    bounds = [(0, max_skew_ns)] * n

    # 3. LP 풀기
    result = scipy.optimize.linprog(c, A_ub=A_ub, b_ub=b_ub, bounds=bounds)

    # 4. 결과 → 버퍼 개수 계산
    for i, ff in enumerate(ff_names):
        skew_ns = result.x[i]
        num_bufs = round(skew_ns / buf_delay_ns)  # 스큐 / 버퍼 딜레이 = 필요 버퍼 수
        output.append((ff, skew_ns, num_bufs))
```

#### AES 벤치마크 규모:
- FF 변수: 535개
- Timing edges: 7,854개
- Negative slack edges: 4,193개
- LP constraints: ~2,571개 (setup + hold)
- scipy.linprog 풀이: < 1초

---

### 2-4. buffer_sizing_iterative.tcl — FFGraph 기반 버퍼 사이징 (Phase 3)

**위치**: `scripts_openroad/buffer_sizing_iterative.tcl`

**What it does**: Phase 2에서 삽입된 작은 버퍼(BUFx2)를 더 큰 버퍼(BUFx4, BUFx6)로 교체(upsize)하여 타이밍 개선.

#### 핵심 흐름:

```tcl
proc run_iterative_buffer_sizing {max_iterations} {
    # 1. FFGraph에서 worst-slack FF 목록 추출
    set ff_list [parse_ffgraph_for_sizing $ff_graph_csv]
    # ff_list = slack 기준 정렬된 FF 목록 (worst first)

    # 2. 각 FF의 clock buffer 찾기
    foreach ff $ff_list {
        set buf [find_ff_clock_buffer $ff]
        # FF의 CLK 핀 → 역추적 → 연결된 clock buffer 찾기
    }

    # 3. Iterative sizing
    set prev_tns [get_tns]

    for {set iter 0} {$iter < $max_iterations} {incr iter} {
        foreach buf $buffer_list {
            # 현재 버퍼의 tier 확인
            set master_name [[$buf getMaster] getName]
            # "BUFx2_ASAP7_75t_R_upper" → tier=upper

            # 한 단계 큰 버퍼로 교체 시도
            # BUFx2 → BUFx3 → BUFx4 → BUFx6 → BUFx6f
            set next_master [get_next_size $master_name]
            # 중요: 같은 tier 내에서만! upper→upper, bottom→bottom

            # swapMaster로 교체
            $buf swapMaster [$db findMaster $next_master]
        }

        # RC 재추출 + 타이밍 업데이트
        detailed_placement
        estimate_parasitics -placement

        set new_tns [get_tns]
        if {$new_tns >= $prev_tns} {
            # TNS 개선 안됨 → 롤백하고 종료
            # 모든 버퍼를 이전 크기로 되돌림
            break
        }
        set prev_tns $new_tns
        puts "Iter $iter: TNS improved to $new_tns"
    }
}
```

#### Tier-Aware 사이징 규칙:
```
Bottom tier 버퍼 크기 순서:
  BUFx2_ASAP7_75t_R_bottom → BUFx3_ASAP7_75t_R_bottom → BUFx4_ASAP7_75t_R_bottom → ...

Upper tier 버퍼 크기 순서:
  BUFx2_ASAP7_75t_R_upper → BUFx3_ASAP7_75t_R_upper → BUFx4_ASAP7_75t_R_upper → ...

절대 cross-tier 교체 안함! (upper 버퍼를 bottom으로 바꾸면 tier 불일치)
```

---

### 2-5. floorplan_utils.tcl — Tier 분류

**위치**: `scripts_openroad/floorplan_utils.tcl`

```tcl
# ODB의 setTier() API를 master name suffix로 채움
proc set_tier_from_master_names {} {
    set block [ord::get_db_block]
    foreach inst [$block getInsts] {
        set master_name [[$inst getMaster] getName]
        if {[string match "*_upper*" $master_name]} {
            $inst setTier 1
        } elseif {[string match "*_bottom*" $master_name]} {
            $inst setTier 0
        }
        # suffix 없으면 tier 미설정 (기본값 유지)
    }
}
```

**이것이 모든 tier 판별의 기초** — CTS 실행 전에 호출되어 ODB에 tier 정보를 채움.
이후 C++에서 `dbInst->getTier()` 로 조회.

---

### 2-6. extract_hbt_delay.tcl — Cross-Tier Delay 측정

**위치**: `scripts_openroad/extract_hbt_delay.tcl`

```tcl
proc extract_cross_tier_delays {} {
    # 1. Parasitics 추출
    estimate_parasitics -placement

    # 2. 모든 net 순회 → driver/sink tier 비교
    foreach net [get_nets *] {
        set driver_tier [get_inst_tier $driver_inst]
        set sink_tier   [get_inst_tier $sink_inst]

        if {$driver_tier != $sink_tier} {
            # Cross-tier net → wire delay 추출
            set delay [get_net_wire_delay $net]
            # CSV 출력: net_name, delay_ps, driver_tier, sink_tier
        }
    }
    # 결과: 평균 HBT delay ≈ 20ps → LP solver의 ΔHB 파라미터
}
```

---

## Part 3: ODB 구조 — Unified Database (Slide 9)

### 현재 구조: Single Merged Tech

```
dbDatabase
  └── dbTech (1개 — merged)
        └── Layers: M1, M2, M2_add, M3, M3_add, M4, ... (양쪽 tier 통합)
  └── dbLib (bottom)
        └── Masters: BUFx2_..._bottom, DFF_..._bottom, ...
  └── dbLib (upper)
        └── Masters: BUFx2_..._upper, DFF_..._upper, ...
  └── dbChip
        └── dbBlock (1개)
              └── dbInst (bottom, tier=0)
              └── dbInst (upper, tier=1)     ← 같은 block에 공존
              └── dbNet (cross-tier 포함)
```

### Tier 정보 흐름:
```
1. LEF/DEF 로드 시 → master name에 _upper/_bottom suffix
2. set_tier_from_master_names() → ODB setTier(0/1) 호출
3. HTreeBuilder → getTier()로 조회 → dominant tier 결정 → tier 버퍼 선택
4. VerilogFFExtractor → master name으로 tier 판별 → CSV에 기록
5. LP solver → CSV의 from_tier/to_tier로 ΔHB 계산
6. Buffer insertion → FF의 tier와 동일한 tier 버퍼 생성
```

### 교수님 질문: Separate Tech LEF 가능?

**ODB가 지원하는 것:**
- `dbDatabase::getTechs()` → 복수 `dbTech` 저장 가능
- `dbTech::create("bottom_tech")` 로 별도 tech 생성 가능

**현실적 제약:**
- `dbBlock` 생성 시 단일 `dbTech`에 바인딩됨
- CTS, STA, Router 등 모든 tool이 단일 tech 가정
- `read_lef`를 두 번 호출하면 중복 레이어 무시됨 (`duplicate LAYER ignored`)

**가능한 방안:**
| 방안 | 설명 | 영향 범위 |
|------|------|----------|
| (A) 현행 유지 | Merged LEF (2A6M7M.lef) | 변경 없음 |
| (B) dbTech 2개 | 각 tier별 별도 tech | ODB + CTS + STA + Router 전부 수정 |
| (C) Layer 속성 추가 | `dbTechLayer`에 `tier` 필드 추가 | ODB만 수정 |

**추천 답변**: "현재 merged LEF 방식이 Pin3D, RosettaStone 2.0에서도 사용하는 표준 방식. 별도 tech LEF 분리는 dbBlock이 two-tech 참조가 필요하며, 모든 tool 수정 필요. 현 단계에서는 merged LEF가 실용적."

---

## Part 4: Workaround 목록

| # | 문제 | Workaround | 위치 |
|---|------|-----------|------|
| 1 | `insert_buffer` ODB crash | `odb::dbInst_create` + `setOrigin` + `setTier` 직접 호출 | `useful_skew.tcl:insert_delay_buffer_odb` |
| 2 | ODB에 tier 정보 없음 (초기 상태) | master name suffix로 `setTier()` 채움 | `floorplan_utils.tcl:set_tier_from_master_names` |
| 3 | CtsOptions에 3D 옵션 없음 | 환경변수 + naming convention으로 우회 | `cts_3d.tcl` (env vars) |
| 4 | Merged tech LEF 필수 | `2A6M7M.lef` 사용 (M2_add, M3_add 포함) | `platform config.mk` |

---

## Part 5: Pin3D 대비 변경 요약

### TCL/Python (flow-3DCTS/scripts_openroad/)
| 파일 | 상태 | 주요 변경 |
|------|------|----------|
| `cts_3d.tcl` | **대폭 수정** | 양쪽 tier 버퍼 제공, Phase 2/3 통합, cross-tier stats |
| `useful_skew.tcl` | **대폭 수정** | LP 기반 재작성, `insert_delay_buffer_odb`, batch+rollback |
| `useful_skew_lp.py` | **대폭 수정** | ΔHB penalty 추가, proper LP formulation |
| `buffer_sizing_iterative.tcl` | **신규** | FFGraph-aware iterative sizing |
| `buffer_sizing.tcl` | 수정 | STA-based endpoint slack extraction |
| `buffer_sizing.py` | 수정 | tier-aware, platform auto-detection |
| `extract_hbt_delay.tcl` | 수정 | RC-based delay 추출 개선 |
| `floorplan_utils.tcl` | 수정 | `set_tier_from_master_names` 추가 |
| `placement_utils.tcl` | 유지 | 기존과 동일 |

### C++ (OpenROAD/src/cts/src/)
| 파일 | 상태 | 주요 변경 |
|------|------|----------|
| `Clock.h` | **수정** | `addSink()` instObj_ fix, ClockInst에 `tier_` 필드 추가 |
| `HTreeBuilder.h` | **수정** | `getDominantTier*()` 3개, `targetTier_` |
| `HTreeBuilder.cpp` | **수정** | `mapBufferMasterToTier()`, tier-aware 버퍼 선택 ~10곳, leaf `setTier()`, branch fallback |
| `VerilogFFExtractor.h` | **신규** | `FFEdgeVerilog` struct + tier fields |
| `VerilogFFExtractor.cpp` | **신규** | Verilog parsing + ODB location + STA timing |
| `TritonCTS.cpp` | **수정** | `extractFFGraphFromVerilog()` 진입점 |

---

## Part 6: ODB TCL Command 가이드 (OpenROAD 셸에서 실행)

OpenROAD를 interactive 모드로 실행한 후 (`openroad` 또는 TCL 스크립트 내에서) 사용 가능한 커맨드들.

### 6-1. 기본: 데이터베이스 접근

```tcl
# DB와 Block 가져오기
set db    [ord::get_db]
set block [ord::get_db_block]

# Tech 정보
set tech [odb::dbDatabase_getTech $db]
set dbu  [odb::dbTech_getDbUnitsPerMicron $tech]
puts "DBU per micron: $dbu"
```

### 6-2. 인스턴스(dbInst) 조회

```tcl
# ===== 전체 인스턴스 목록 =====
set all_insts [$block getInsts]
puts "Total instances: [llength $all_insts]"

# ===== 이름으로 검색 =====
set inst [$block findInst "_16693__upper"]
if {$inst ne "NULL"} {
    puts "Found: [$inst getName]"
}

# ===== 인스턴스 기본 정보 =====
set inst [$block findInst "_16693__upper"]
puts "Name:   [$inst getName]"
puts "Master: [[$inst getMaster] getName]"     ;# 예: DFFHQNx1_ASAP7_75t_R_upper
puts "Tier:   [$inst getTier]"                 ;# 0=bottom, 1=upper
puts "Status: [$inst getPlacementStatus]"      ;# PLACED, FIRM, etc.

# 위치 (DBU 단위)
set box [$inst getBBox]
puts "Location: ([$box xMin], [$box yMin]) - ([$box xMax], [$box yMax])"

# 위치 (um 단위)
set x_um [expr {[$box xMin] / double($dbu)}]
set y_um [expr {[$box yMin] / double($dbu)}]
puts "Location (um): ($x_um, $y_um)"
```

### 6-3. Tier별 인스턴스 분류 / 카운팅

```tcl
# ===== Tier별 인스턴스 수 세기 =====
set tier0 0; set tier1 0; set no_tier 0
foreach inst [$block getInsts] {
    set t [$inst getTier]
    if {$t == 0} { incr tier0
    } elseif {$t == 1} { incr tier1
    } else { incr no_tier }
}
puts "Bottom(tier=0): $tier0"
puts "Upper(tier=1):  $tier1"
puts "Unset:          $no_tier"

# ===== FF만 필터링 (master name에 DFF 포함) =====
set ff_count 0
foreach inst [$block getInsts] {
    set mname [[$inst getMaster] getName]
    if {[string match "*DFF*" $mname]} {
        incr ff_count
        # 상세 출력 (처음 5개만)
        if {$ff_count <= 5} {
            puts "FF: [$inst getName] | master=$mname | tier=[$inst getTier]"
        }
    }
}
puts "Total FFs: $ff_count"

# ===== 버퍼만 필터링 (master name에 BUF 포함) =====
set buf_bottom 0; set buf_upper 0
foreach inst [$block getInsts] {
    set mname [[$inst getMaster] getName]
    if {[string match "*BUF*" $mname]} {
        if {[string match "*_upper*" $mname]} { incr buf_upper
        } elseif {[string match "*_bottom*" $mname]} { incr buf_bottom }
    }
}
puts "Bottom buffers: $buf_bottom"
puts "Upper buffers:  $buf_upper"
```

### 6-4. 넷(dbNet) 조회

```tcl
# ===== 전체 넷 수 =====
puts "Total nets: [llength [$block getNets]]"

# ===== 특정 넷 조회 =====
set net [$block findNet "clk"]
puts "Net: [$net getName] | SigType: [$net getSigType]"

# ===== 넷에 연결된 인스턴스 조회 =====
foreach iterm [$net getITerms] {
    set inst [$iterm getInst]
    set pin_name [[$iterm getMTerm] getName]
    puts "  [$inst getName] / $pin_name (tier=[$inst getTier])"
}

# ===== Cross-tier 넷 찾기 =====
set cross_tier_nets 0
foreach net [$block getNets] {
    set sig [$net getSigType]
    if {$sig eq "POWER" || $sig eq "GROUND"} { continue }

    set tiers {}
    foreach iterm [$net getITerms] {
        set t [[$iterm getInst] getTier]
        if {$t >= 0} { lappend tiers $t }
    }
    set unique [lsort -unique $tiers]
    if {[llength $unique] > 1} {
        incr cross_tier_nets
        # 처음 3개만 출력
        if {$cross_tier_nets <= 3} {
            puts "Cross-tier net: [$net getName] (tiers: $unique)"
        }
    }
}
puts "Total cross-tier nets: $cross_tier_nets"
```

### 6-5. 인스턴스 생성 / 수정 / 삭제

```tcl
# ===== 버퍼 생성 (직접 ODB API) =====
set db [ord::get_db]
set block [ord::get_db_block]

# 1. Master 찾기
set master_name "BUFx2_ASAP7_75t_R_upper"
set master ""
foreach lib [$db getLibs] {
    set m [$lib findMaster $master_name]
    if {$m ne "NULL"} { set master $m; break }
}
puts "Found master: [$master getName]"

# 2. 인스턴스 생성
set new_buf [odb::dbInst_create $block $master "my_test_buffer"]
$new_buf setLocation 1000 2000        ;# DBU 좌표
$new_buf setPlacementStatus "PLACED"
$new_buf setTier 1                    ;# upper tier
puts "Created: [$new_buf getName] at tier [$new_buf getTier]"

# ===== Master 교체 (buffer sizing) =====
set bigger_master ""
foreach lib [$db getLibs] {
    set m [$lib findMaster "BUFx4_ASAP7_75t_R_upper"]
    if {$m ne "NULL"} { set bigger_master $m; break }
}
$new_buf swapMaster $bigger_master
puts "Swapped to: [[$new_buf getMaster] getName]"

# ===== Tier 변경 =====
$new_buf setTier 0   ;# bottom으로 변경
puts "New tier: [$new_buf getTier]"

# ===== 인스턴스 삭제 =====
odb::dbInst_destroy $new_buf
puts "Buffer destroyed"
```

### 6-6. 핀(ITerm) 연결 조작

```tcl
# ===== 인스턴스의 핀 목록 =====
set inst [$block findInst "_16693__upper"]
foreach iterm [$inst getITerms] {
    set mterm [$iterm getMTerm]
    set pin_name [$mterm getName]
    set io_type  [$mterm getIoType]     ;# INPUT, OUTPUT
    set sig_type [$mterm getSigType]    ;# SIGNAL, CLOCK, POWER, GROUND
    set net_name ""
    set net [$iterm getNet]
    if {$net ne "NULL"} { set net_name [$net getName] }
    puts "  Pin: $pin_name ($io_type, $sig_type) → net: $net_name"
}

# ===== 넷 연결 변경 (버퍼 삽입 패턴) =====
# FF의 CLK 핀에서 기존 넷을 끊고 → 새 넷으로 연결
set ff_inst [$block findInst "some_ff"]
set clk_iterm [$ff_inst findITerm "CLK"]
set old_net [$clk_iterm getNet]
puts "Old net: [$old_net getName]"

# 새 넷 생성
set new_net [odb::dbNet_create $block "skew_net_123"]

# 연결 변경
$clk_iterm disconnect           ;# 기존 넷에서 분리
$clk_iterm connect $new_net     ;# 새 넷에 연결
puts "Now connected to: [[$clk_iterm getNet] getName]"

# 넷 삭제
# odb::dbNet_destroy $new_net
```

### 6-7. 타이밍 정보 조회

```tcl
# ===== 기본 타이밍 보고 =====
report_tns                  ;# Total Negative Slack
report_wns                  ;# Worst Negative Slack

# ===== 상세 타이밍 경로 =====
report_checks -path_delay max -slack_max 0 -group_count 10

# ===== 프로그래밍적으로 TNS/WNS 가져오기 =====
set tns [sta::total_negative_slack_cmd "max"]
set wns [sta::worst_slack_cmd "max"]
puts "TNS: $tns ns"
puts "WNS: $wns ns"

# ===== Parasitics 재추출 후 타이밍 업데이트 =====
estimate_parasitics -placement
report_tns
report_wns
```

### 6-8. Library/Master 조회

```tcl
# ===== 로드된 라이브러리 목록 =====
foreach lib [$db getLibs] {
    puts "Library: [$lib getName] ([llength [$lib getMasters]] masters)"
}

# ===== 특정 master 찾기 =====
foreach lib [$db getLibs] {
    set m [$lib findMaster "BUFx4_ASAP7_75t_R_upper"]
    if {$m ne "NULL"} {
        puts "Found [$m getName] in [$lib getName]"
        puts "  Width:  [$m getWidth] DBU"
        puts "  Height: [$m getHeight] DBU"
        puts "  Type:   [$m getType]"       ;# CORE, BLOCK, PAD, etc.
        break
    }
}

# ===== 사용 가능한 버퍼 master 전체 목록 =====
foreach lib [$db getLibs] {
    foreach master [$lib getMasters] {
        set mname [$master getName]
        if {[string match "*BUF*" $mname] && [string match "*CLKBUF*" $mname]} {
            puts "  $mname"
        }
    }
}
```

### 6-9. Tech Layer 조회

```tcl
# ===== 레이어 목록 =====
set tech [odb::dbDatabase_getTech $db]
foreach layer [$tech getLayers] {
    set lname [$layer getName]
    set ltype [$layer getType]          ;# ROUTING, CUT, MASTERSLICE, etc.
    set rl [$layer getRoutingLevel]     ;# 0이면 non-routing
    if {$ltype eq "ROUTING"} {
        puts "Layer: $lname (routing level $rl)"
    }
}
```

### 6-10. 한줄 요약: 자주 쓰는 패턴

```tcl
# 인스턴스 정보 한줄
puts "[[$inst getMaster] getName] tier=[$inst getTier] at [[$inst getBBox] xMin],[[$inst getBBox] yMin]"

# 넷의 driver 찾기 (OUTPUT 핀)
foreach iterm [$net getITerms] {
    if {[[$iterm getMTerm] getIoType] eq "OUTPUT"} {
        puts "Driver: [[$iterm getInst] getName]"
    }
}

# 넷의 fanout 수
puts "Fanout: [llength [$net getITerms]]"
```

### 실행 방법

```bash
# OpenROAD interactive 모드로 진입
openroad

# 또는 디자인 로드 후 interactive
openroad -gui   # GUI 포함

# TCL 스크립트에서 사용 (디자인 로드 후)
source scripts_openroad/load.tcl
load_design 3_place.v 3_place.sdc "Loading..."
# 이후 위 커맨드들 사용 가능
```

---

## V31: Useful-Skew Wire Length Adjustment (2026-02-23)

### 핵심 아이디어 (Fishburn 1990 기반)

**V30b 한계**: balanced H-tree 완성 후 delay buffer를 덧붙이는 post-hoc patch.
- TritonCTS는 여전히 zero-skew balanced tree 생성 (equal wirelength to all sinks)
- LP skew targets가 clustering에만 영향 (step3), delay buffer 수 조정에만 영향 (step4)

**V31 아이디어**: LP 결과로 tree 자체를 처음부터 불균등하게 구성.

### 이론적 배경

Fishburn (1990) LP-SAFETY: 각 FF의 clock delay `x_i`를 변수로 설정하여
- setup/hold constraint를 만족하면서 최소 safety margin M을 최대화
- 실제 회로에서는 clock buffer + wire로 `x_i` 실현

**V31 핵심 변경**: k-means converge 후 branching point를 radially shift:

```
shift_um = wireSkewScale × (T_cluster - T_global) / wireDelayPerUnit

T_cluster > T_global → branch farther from root → longer wire → later clock
T_cluster < T_global → branch closer to root   → shorter wire → earlier clock
```

### 변경된 파일

#### `OpenROAD_v31/src/cts/src/HTreeBuilder.h`
- Added: `wireSkewScale_` (from `CTS_WIRE_SKEW_SCALE` env, default 1.0)
- Added: `wireDelayPerUnit_` (from `CTS_WIRE_DELAY_PS_UM` env, default 1.0 ps/um)

#### `OpenROAD_v31/src/cts/src/HTreeBuilder.cpp`
- `run()`: Initialize `wireSkewScale_` and `wireDelayPerUnit_` from env vars
- `refineBranchingPointsWithClustering()`: After k-means, compute per-cluster mean LP target,
  then shift each branch point radially from root:
  ```cpp
  shiftBranchPoint(branchPt1, meanT0 - globalMean);
  shiftBranchPoint(branchPt2, meanT1 - globalMean);
  ```
  Clamped to ±50% of current segment length.

#### `scripts_openroad/cts_skew_lp.py`
- Added LP-SAFETY mode (`--lp-mode safety`, default): maximize minimum margin M
  vs LP-SPEED (original): maximize total slack improvement sum
- Added clock uncertainty model: `sigma_local` (same-tier) + `sigma_hb` (upper-tier HB path)
  Effective slack = nominal_slack - sigma_i - sigma_j per Fishburn CMOS-LP-SAFETY

#### `scripts_openroad/cts_3d.tcl`
- Phase 0: Pass `--lp-mode`, `--sigma-local`, `--sigma-hb` to LP solver
- Fixed: NULL getMaster guard in count_cross_tier_nets and report_cross_tier_stats

### 주요 파라미터

| Env Var | Default | 설명 |
|---------|---------|------|
| `CTS_WIRE_SKEW_SCALE` | 1.0 | Wire shift 강도 (0=zero-skew 유지) |
| `CTS_WIRE_DELAY_PS_UM` | 1.0 | Wire delay per unit length (ps/um) |
| `PRE_CTS_LP_MODE` | safety | LP formulation: safety=maximin, speed=sum |
| `PRE_CTS_SIGMA_LOCAL` | 0.005 | Same-tier clock uncertainty (ns) |
| `PRE_CTS_SIGMA_HB` | 0.005 | HB crossing additional uncertainty (ns) |

### 비교 대상

| | V30 | V30a | V30b | V31 | **V32** |
|---|---|---|---|---|---|
| Pre-CTS LP | LP-SPEED | LP-SPEED | LP-SPEED | LP-SAFETY | **LP-SAFETY V32** |
| LP bounds | sym [-s,+s] | sym | sym | sym [-s,+s] | **[0, s+t_via]** |
| sigma_hb | - | - | - | 0.005ns | **removed** |
| z_i vars | yes | yes | yes | yes | **removed** |
| Estimator | - | - | - | - | **ClockLatencyEstimator** |
| CTS Step | 1+2 only | 1+2+3 | 1+2+3+4 | 1+2+3+4+wire | **1+2+3+4** |
| OpenROAD | step12 | step123 | step1234 | v31 | **v32** |

---

## V32: Non-negative LP-SAFETY + ClockLatencyEstimator (2026-02-23)

### 핵심 변경사항

V31의 LP-SAFETY에서 두 가지 근본적 문제를 수정:

1. **LP 변수 범위 문제**: V31 `a_i ∈ [-max_skew, +max_skew]` → normalize 후 `delay_i = a_i - min(a_j)` 최대 `2*max_skew`가 필요해서 실제 구현 불가능
   - **V32 수정**: `a_i ∈ [0, max_skew + t_via]` (non-negative, add-only delay)
   - 물리적으로 실현 가능: 항상 delay만 추가, clock advancement 불필요

2. **sigma_hb 제거**: Via delay가 STA parasitic으로 이미 계산되므로 jitter 항 불필요

### Professor's Architecture (Bottom-*)

세 가지 leaf buffer case의 union으로 achievable range 계산:
- **Case 1**: single bottom leaf → bottom FF direct, top FF via HB
- **Case 2**: single top leaf → top FF direct, bottom FF via HB
- **Case 3**: per-tier leaf buffers → 각 tier 독립 제어

Union 결과: 모든 FF에 대해 `a_i ∈ [0, max_skew + t_via]`

### 새로운 C++ 클래스: ClockLatencyEstimator

#### `OpenROAD_v32/src/cts/src/ClockLatencyEstimator.h` (신규)
- `estimateAndWrite(output_csv, max_skew_ns)`: per-FF 물리적 달성 가능 범위 계산
- `getViaDelayNs()`: `t_via = 0.693 × R_HB × C_HB` (Elmore 50% delay)
- Cts3DDatabase에서 HBT R/C 읽기

#### `OpenROAD_v32/src/cts/src/ClockLatencyEstimator.cpp` (신규)
- 출력 CSV: `ff_name,tier,t_min_ns,t_max_ns`
- bottom-tier FF: `t_max = max_skew`
- upper-tier FF: `t_max = max_skew + t_via`

#### `OpenROAD_v32/src/cts/src/CMakeLists.txt` (수정)
- `ClockLatencyEstimator.cpp` → `cts_lib`에 추가

#### `OpenROAD_v32/src/cts/include/cts/TritonCTS.h` (수정)
- `estimateLeafLatencies(output_csv, max_skew_ns)` 메서드 추가

#### `OpenROAD_v32/src/cts/src/TritonCTS.i` (수정)
- `estimate_leaf_latencies(output_csv, max_skew_ns)` Tcl 명령 추가

#### `OpenROAD_v32/src/cts/src/TritonCTS.cpp` (수정)
- `estimateLeafLatencies()` 구현: cts3dDb_ 초기화 → HBT 로드 → estimator 실행

### LP-SAFETY V32 변경사항 (cts_skew_lp.py)

| 항목 | V31 | V32 |
|------|-----|-----|
| 변수 수 | `2*n_ffs + 1` (a, z, M) | `n_ffs + 1` (a, M) |
| a_i 범위 | `[-max_skew, +max_skew]` | `[0, t_max_i]` |
| sigma_hb | 0.005ns (upper-tier) | **removed** |
| z_i | `|a_i|` linearization | **removed** (a_i ≥ 0) |
| pruning | `eff_slack > 2*max_skew` | `eff_slack > t_max_i + t_max_j` |
| `--bounds-csv` | N/A | per-FF bounds from estimator |
| `--sigma-hb` | yes | **removed** |

### cts_3d.tcl Phase 0 변경 (V32)

```
Phase 0 (V32):
  0a: cts::estimate_leaf_latencies $bounds_csv $max_skew_ns  ← NEW (C++)
  0b: estimate_parasitics -placement
  0c: cts::extract_ff_timing_graph_verilog ... → timing_csv
  0d: python3 cts_skew_lp.py ... --bounds-csv $bounds_csv  ← NEW arg, no --sigma-hb
  0e: cts::load_skew_targets $targets_csv
```

### 주요 파라미터 (V32)

| Env Var | Default | 설명 |
|---------|---------|------|
| `PRE_CTS_MAX_SKEW` | 0.100 | per-FF delay budget (ns), shared with estimator |
| `PRE_CTS_LP_MODE` | safety | LP formulation: safety=V32 maximin |
| `PRE_CTS_SIGMA_LOCAL` | 0.005 | Same-tier clock uncertainty (ns) |
| ~~`PRE_CTS_SIGMA_HB`~~ | ~~0.005~~ | **V32에서 제거** (via RC는 STA에서 처리) |

