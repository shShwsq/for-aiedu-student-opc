# ACP(Qoder/dsh/Codex CLI 执行器)首响应延迟分析
# 数据源:backend/logs/perf.log(由 app/perf.py 打点)
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File backend/scripts/perf_acp_report.ps1
#
# 输出:
# - bridge reuse rate: 会话复用命中率(miss = 走 ~20s 冷启动链路)
# - acp_enter -> acp_prompt_send: CLI 握手链耗时,按复用命中/未命中拆分
# - per-stage cost: 各打点阶段耗时分布
# - e2e: submit/resume -> 首个可见输出

$p = Join-Path (Split-Path (Split-Path $PSCommandPath -Parent)) "logs/perf.log"
$txt = Get-Content $p

$hit = ($txt | Select-String 'acp_bridge_reuse.*hit=True').Count
$miss = ($txt | Select-String 'acp_bridge_reuse.*hit=False').Count
if (($hit + $miss) -gt 0) {
    Write-Output ("bridge reuse: hit={0} miss={1} rate={2}%" -f $hit, $miss,
        [math]::Round(100 * $hit / ($hit + $miss), 1))
}

function Stat($name, $vals) {
    if ($vals.Count -eq 0) { return }
    $s = $vals | Sort-Object
    Write-Output ("{0}: n={1} median={2}s p90={3}s max={4}s avg={5}s" -f $name, $s.Count,
        $s[[int]($s.Count * 0.5)], $s[[int]([Math]::Min($s.Count * 0.9, $s.Count - 1))],
        ($s | Measure-Object -Maximum).Maximum,
        [math]::Round(($s | Measure-Object -Average).Average, 2))
}

function ToTime($ts) { [datetime]::ParseExact($ts, 'yyyy-MM-dd HH:mm:ss.fff', $null) }

# --- acp_enter -> acp_prompt_send,按当轮复用命中与否拆分 ---
$state = @{}
$hitGaps = @(); $missGaps = @()
foreach ($l in $txt) {
    if ($l -match 'ts=([\d\- :\.]+) task=(\S+) stage=acp_enter') {
        $state[$Matches[2]] = @{t = (ToTime $Matches[1]); hit = $null }
    }
    elseif ($l -match 'ts=([\d\- :\.]+) task=(\S+) stage=acp_bridge_reuse cost=[\d.]+s \S+ hit=(True|False)') {
        if ($state.ContainsKey($Matches[2])) { $state[$Matches[2]].hit = $Matches[3] }
    }
    elseif ($l -match 'ts=([\d\- :\.]+) task=(\S+) stage=acp_prompt_send') {
        $k = $Matches[2]
        if ($state.ContainsKey($k)) {
            $d = [math]::Round(((ToTime $Matches[1]) - $state[$k].t).TotalSeconds, 2)
            if ($state[$k].hit -eq 'True') { $hitGaps += $d } else { $missGaps += $d }
            $state.Remove($k)
        }
    }
}
Write-Output ""
Write-Output "=== acp_enter -> acp_prompt_send (CLI handshake chain) ==="
Stat "reuse-hit" $hitGaps
Stat "cold-rebuild" $missGaps

# --- 各阶段耗时(过滤 cost<=0.005s 的测试桩) ---
Write-Output ""
Write-Output "=== per-stage cost ==="
foreach ($st in @("prepare_repo_context", "acp_wait_bridge_ready", "acp_initialize",
                  "acp_new_session", "acp_session_open", "acp_first_event", "acp_bridge_reuse")) {
    $v = @()
    foreach ($l in $txt) {
        if ($l -match ('stage=' + [regex]::Escape($st) + ' cost=([\d.]+)s')) {
            $d = [double]$Matches[1]
            if ($d -gt 0.005) { $v += $d }
        }
    }
    Stat $st $v
}

# --- submit/resume -> 首个可见输出 ---
$anchor = @{}; $e2e = @()
foreach ($l in $txt) {
    if ($l -match 'ts=([\d\- :\.]+) task=(\S+) stage=(task_start|resume_start)') {
        $anchor[$Matches[2]] = ToTime $Matches[1]
    }
    elseif ($l -match 'ts=([\d\- :\.]+) task=(\S+) stage=acp_first_event cost=') {
        $k = $Matches[2]
        if ($anchor.ContainsKey($k)) {
            $e2e += [math]::Round(((ToTime $Matches[1]) - $anchor[$k]).TotalSeconds, 2)
            $anchor.Remove($k)
        }
    }
}
Write-Output ""
Write-Output "=== submit/resume -> first visible output (e2e) ==="
Stat "e2e" $e2e
