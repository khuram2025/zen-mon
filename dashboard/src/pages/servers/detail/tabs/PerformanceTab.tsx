/** Performance: every resource over a chosen window.
 *
 *  CPU (user/system split, processor queue, per-core heatmap), memory
 *  (used, committed, cached, page file), each disk (throughput, IOPS,
 *  latency, utilisation), each volume (usage, growth, forecast) and each
 *  network interface (throughput, packets, errors). All charts share one
 *  crosshair, so a spike can be traced across resources at a glance. */

import { useMemo, useState } from 'react'
import { Activity, CheckCircle2, Cpu, Gauge, HardDrive, MemoryStick, Network as NetworkIcon, Timer } from 'lucide-react'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'
import { cn, formatBps, formatBytes, timeAxisTickFormatter } from '@/lib/utils'
import type { ServerItem } from '@/types/servers'
import { isPseudoInterface, toRows, useMetricDetail, type MetricDetail } from '../data'
import {
  C, ChartStats, EmptyState, Meter, Panel, Segmented, TimeSeries, fmtMs, fmtNum, fmtPct, pctTone, stats,
} from '../ui'

const SECTIONS = [
  { key: 'cpu', label: 'CPU', icon: Cpu },
  { key: 'memory', label: 'Memory', icon: MemoryStick },
  { key: 'disks', label: 'Disks', icon: Gauge },
  { key: 'volumes', label: 'Volumes', icon: HardDrive },
  { key: 'network', label: 'Network', icon: NetworkIcon },
] as const

function SectionTitle({ id, icon: IconC, title, hint }: {
  id: string
  icon: React.ComponentType<{ className?: string }>
  title: string
  hint?: string
}) {
  return (
    <div id={`perf-${id}`} className="flex scroll-mt-32 items-center gap-2.5 pt-3">
      <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
        <IconC className="h-4 w-4" />
      </span>
      <div>
        <h2 className="text-base font-semibold tracking-tight">{title}</h2>
        {hint && <p className="text-xs text-muted">{hint}</p>}
      </div>
    </div>
  )
}

/** Per-core usage over time as a heatmap: rows are cores, columns time. */
function CoreHeatmap({ detail, tick }: { detail: MetricDetail; tick: (ts: number) => string }) {
  const { columns, coreCount } = useMemo(() => {
    const series = detail.cores.series
    const width = 72
    const step = Math.max(1, Math.ceil(series.length / width))
    const cols: { ts: number; values: number[] }[] = []
    let n = 0
    for (let i = 0; i < series.length; i += step) {
      const chunk = series.slice(i, i + step)
      const len = Math.max(...chunk.map((c) => c.values.length))
      n = Math.max(n, len)
      const values = Array.from({ length: len }, (_, core) => {
        const vals = chunk.map((c) => c.values[core]).filter((v): v is number => v != null)
        return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0
      })
      cols.push({ ts: Date.parse(chunk[0].ts), values })
    }
    return { columns: cols, coreCount: n }
  }, [detail.cores.series])

  if (!columns.length || !coreCount) return <EmptyState icon={Cpu} title="Per-core data not reported" compact />
  const labelEvery = coreCount > 16 ? 4 : 1
  // Fill about the height of the neighbouring chart whatever the core count.
  const rowH = Math.max(6, Math.min(26, Math.floor(190 / coreCount)))
  return (
    <div>
      <div className="flex gap-2">
        <div className="flex flex-col justify-between py-px text-2xs tabular-nums leading-none text-muted" style={{ height: coreCount * (rowH + 1) }}>
          {Array.from({ length: coreCount }, (_, i) => (
            <span key={i} className="flex items-center" style={{ height: rowH + 1 }}>{i % labelEvery === 0 ? i : ''}</span>
          ))}
        </div>
        <div
          className="grid flex-1 gap-px overflow-hidden rounded-md"
          style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(0, 1fr))`, gridTemplateRows: `repeat(${coreCount}, ${rowH}px)`, gridAutoFlow: 'column' }}
        >
          {columns.map((col) =>
            Array.from({ length: coreCount }, (_, core) => {
              const v = col.values[core] ?? 0
              const color = v >= 90 ? 'rgb(var(--danger))' : v >= 75 ? 'rgb(var(--warning))' : `rgb(var(--primary) / ${0.1 + (v / 100) * 0.9})`
              return (
                <div
                  key={`${col.ts}-${core}`}
                  title={`Core ${core} · ${new Date(col.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} · ${v.toFixed(1)}%`}
                  style={{ background: color }}
                />
              )
            }),
          )}
        </div>
      </div>
      <div className="mt-1.5 flex justify-between pl-6 text-2xs text-muted">
        <span>{tick(columns[0].ts)}</span>
        <span>{tick(columns[Math.floor(columns.length / 2)].ts)}</span>
        <span>{tick(columns[columns.length - 1].ts)}</span>
      </div>
      <div className="mt-2 flex items-center gap-2 text-2xs text-muted">
        <span>0%</span>
        <div className="h-1.5 w-28 rounded-full" style={{ background: 'linear-gradient(90deg, rgb(var(--primary) / 0.1), rgb(var(--primary)))' }} />
        <span>75%</span>
        <span className="h-1.5 w-3 rounded-sm bg-warning" />
        <span>90%+</span>
        <span className="h-1.5 w-3 rounded-sm bg-danger" />
      </div>
    </div>
  )
}

export function PerformanceTab({ server }: { server: ServerItem }) {
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const { data: detail, isLoading, isError, isFetching } = useMetricDetail(server.id, range.fromISO, range.toISO, !isCustom)
  const tick = timeAxisTickFormatter(range.hours)
  const [disk, setDisk] = useState<string>('')
  const [nic, setNic] = useState<string>('')

  const jump = (key: string) => document.getElementById(`perf-${key}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })

  const toolbar = (
    <div className="sticky top-[45px] z-10 -mx-1 flex flex-wrap items-center justify-between gap-3 border-b border-border/60 bg-bg/90 px-1 py-2 backdrop-blur supports-[backdrop-filter]:bg-bg/75">
      <div className="flex flex-wrap items-center gap-1">
        {SECTIONS.map((s) => (
          <button
            key={s.key}
            type="button"
            onClick={() => jump(s.key)}
            className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium text-muted transition-colors hover:bg-surface2 hover:text-text"
          >
            <s.icon className="h-3.5 w-3.5" /> {s.label}
          </button>
        ))}
      </div>
      <div className="flex items-center gap-3">
        <span className={cn('text-xs text-muted transition-opacity', isFetching ? 'opacity-100' : 'opacity-0')}>Updating…</span>
        <TimeRangePicker
          rangeIdx={rangeIdx} isCustom={isCustom}
          customFrom={range.fromISO} customTo={range.toISO}
          onPreset={setPreset} onCustom={setCustom}
        />
      </div>
    </div>
  )

  if (isError) {
    return (
      <div className="space-y-4">
        {toolbar}
        <Panel><EmptyState icon={Activity} title="Metrics are unavailable" hint="The metric store did not answer. Try again in a moment." /></Panel>
      </div>
    )
  }
  if (isLoading || !detail) {
    return (
      <div className="space-y-4">
        {toolbar}
        <div className="grid gap-4 lg:grid-cols-2">
          {Array.from({ length: 4 }).map((_, i) => <div key={i} className="h-72 animate-pulse rounded-xl border border-border bg-surface" />)}
        </div>
      </div>
    )
  }
  if (!detail.cpu.length && !detail.memory.length) {
    return (
      <div className="space-y-4">
        {toolbar}
        <Panel>
          <EmptyState
            icon={Activity}
            title={`No metrics for ${range.label.toLowerCase()}`}
            hint={server.agent_status === 'online'
              ? 'The agent is online but is not reporting server metrics. Check that server monitoring is enabled in its monitoring profile.'
              : 'Metrics appear once an authorized agent reports. Try a longer time range.'}
          />
        </Panel>
      </div>
    )
  }

  /* CPU */
  const hasSplit = detail.cpu.some((p) => p.user != null && p.user > 0)
  const cpuRows = toRows(detail.cpu, (p) => ({ total: p.total, user: p.user, system: p.system, peak: p.peak }))
  const cpuStats = stats(detail.cpu.map((p) => p.total))
  const queueRows = toRows(detail.cpu, (p) => ({ queue: p.queue }))
  const queueStats = stats(detail.cpu.map((p) => p.queue))
  const cores = server.cpu_cores || detail.cores.latest.length || 1

  /* Memory */
  const memRows = toRows(detail.memory, (p) => ({ used: p.used, committed: p.committed, cached: p.cached, total: p.total }))
  const memPct = stats(detail.memory.map((p) => p.used_pct))
  const lastMem = detail.memory[detail.memory.length - 1]
  const hasSwap = detail.memory.some((p) => (p.swap_total ?? 0) > 0)
  const swapRows = toRows(detail.memory, (p) => ({
    swap: p.swap_total ? ((p.swap_used ?? 0) / p.swap_total) * 100 : null,
  }))

  /* Disks */
  const diskNames = detail.disks.map((d) => d.device)
  const activeDisk = detail.disks.find((d) => d.device === disk) ?? detail.disks[0]
  const dPoints = activeDisk?.points ?? []
  const diskThroughput = toRows(dPoints, (p) => ({ read: p.read_bps, write: p.write_bps }))
  const diskIops = toRows(dPoints, (p) => ({ read: p.read_iops, write: p.write_iops }))
  const diskLatency = toRows(dPoints, (p) => ({ read: p.read_ms, write: p.write_ms }))
  const diskUtil = toRows(dPoints, (p) => ({ util: p.util_pct, queue: p.queue }))
  const latencyStats = stats(dPoints.map((p) => Math.max(p.read_ms ?? 0, p.write_ms ?? 0)))
  const utilStats = stats(dPoints.map((p) => p.util_pct))
  // Stored as zero when the agent does not report them (Windows agents
  // report throughput and operations only), so all-zero means unreported.
  // The Windows agent's disk timings come from a library that truncates
  // them to whole seconds, so they are noise rather than latency.
  const latencyTrusted = server.os_type !== 'windows'
  const hasLatency = latencyTrusted && dPoints.some((p) => (p.read_ms ?? 0) > 0 || (p.write_ms ?? 0) > 0)
  const hasBusy = dPoints.some((p) => (p.util_pct ?? 0) > 0 || (p.queue ?? 0) > 0)

  /* Volumes */
  const volumeRows = (() => {
    const byTs = new Map<number, Record<string, number | null>>()
    for (const fs of detail.filesystems) {
      for (const p of fs.points) {
        const ts = Date.parse(p.ts)
        const row = byTs.get(ts) ?? { ts }
        row[fs.mount] = p.used_pct
        byTs.set(ts, row)
      }
    }
    return [...byTs.values()].sort((a, b) => (a.ts as number) - (b.ts as number))
  })()
  const volumeColors = [C.teal, C.blue, C.violet, C.orange, C.sky, C.green, C.red]

  /* Network */
  const realNics = detail.interfaces.filter((i) => !isPseudoInterface(i.name) && i.points.some((p) => (p.rx_bytes_ps ?? 0) + (p.tx_bytes_ps ?? 0) > 0))
  const nicList = realNics.length ? realNics : detail.interfaces
  const busiest = [...nicList].sort((a, b) => {
    const sum = (x: typeof a) => x.points.reduce((s, p) => s + (p.rx_bytes_ps ?? 0) + (p.tx_bytes_ps ?? 0), 0)
    return sum(b) - sum(a)
  })[0]
  const activeNic = nicList.find((i) => i.name === nic) ?? busiest
  const nPoints = activeNic?.points ?? []
  const nicThroughput = toRows(nPoints, (p) => ({ rx: (p.rx_bytes_ps ?? 0) * 8, tx: (p.tx_bytes_ps ?? 0) * 8 }))
  const nicPackets = toRows(nPoints, (p) => ({ rx: p.rx_pps, tx: p.tx_pps }))
  const nicErrors = toRows(nPoints, (p) => ({ errors: p.errors_ps, drops: p.drops_ps }))
  const rxStats = stats(nPoints.map((p) => (p.rx_bytes_ps ?? 0) * 8))
  const txStats = stats(nPoints.map((p) => (p.tx_bytes_ps ?? 0) * 8))
  const errorTotal = nPoints.reduce((s, p) => s + (p.errors_ps ?? 0) + (p.drops_ps ?? 0), 0)
  const hasPackets = nPoints.some((p) => (p.rx_pps ?? 0) + (p.tx_pps ?? 0) > 0)

  return (
    <div className="space-y-4">
      {toolbar}

      {/* CPU */}
      <SectionTitle id="cpu" icon={Cpu} title="CPU" hint={`${cores} logical core${cores === 1 ? '' : 's'}${server.cpu_model ? ` · ${server.cpu_model}` : ''}`} />
      <Panel title="Utilisation" subtitle={hasSplit ? 'User and system time' : 'Total'}>
        <ChartStats
          items={[
            { label: 'Current', value: fmtPct(cpuStats?.current), tone: pctTone(cpuStats?.current) },
            { label: 'Average', value: fmtPct(cpuStats?.avg) },
            { label: '95th percentile', value: fmtPct(cpuStats?.p95), tone: pctTone(cpuStats?.p95) },
            { label: 'Peak', value: fmtPct(cpuStats?.peak), tone: pctTone(cpuStats?.peak) },
          ]}
        />
        <TimeSeries
          data={cpuRows}
          series={hasSplit
            ? [
                { key: 'user', label: 'User', color: C.blue, stack: 'cpu' },
                { key: 'system', label: 'System', color: C.violet, stack: 'cpu' },
                { key: 'peak', label: 'Peak in interval', color: C.slate, line: true, dashed: true },
              ]
            : [{ key: 'total', label: 'CPU', color: C.blue }]}
          height={240}
          yFormat={(v) => `${v.toFixed(0)}%`}
          yDomain={[0, 100]}
          tickFormat={tick}
          references={[{ y: 90, label: '90%', tone: 'danger' }]}
        />
      </Panel>
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Processor queue" subtitle={`Threads waiting for a CPU · sustained above ${cores * 2} indicates contention`} icon={Timer}>
          <ChartStats
            items={[
              { label: 'Current', value: fmtNum(queueStats?.current) },
              { label: 'Average', value: fmtNum(queueStats?.avg) },
              { label: 'Peak', value: fmtNum(queueStats?.peak), tone: (queueStats?.peak ?? 0) > cores * 2 ? 'warning' : 'default' },
              { label: 'Cores', value: cores },
            ]}
          />
          <TimeSeries
            data={queueRows}
            series={[{ key: 'queue', label: 'Queue length', color: C.orange }]}
            height={170}
            yFormat={(v) => fmtNum(v)}
            tickFormat={tick}
            references={[{ y: cores * 2, label: `${cores * 2} (2 × cores)`, tone: 'warning' }]}
          />
        </Panel>
        <Panel title="Per-core load" subtitle="Each row is a logical core" icon={Cpu}>
          <CoreHeatmap detail={detail} tick={tick} />
        </Panel>
      </div>

      {/* Memory */}
      <SectionTitle
        id="memory" icon={MemoryStick} title="Memory"
        hint={lastMem?.total ? `${formatBytes(lastMem.total)} physical${hasSwap && lastMem.swap_total ? ` · ${formatBytes(lastMem.swap_total)} page file` : ''}` : undefined}
      />
      <Panel title="Usage" subtitle="Used, committed and cache">
        <ChartStats
          items={[
            { label: 'Used', value: lastMem?.used != null ? `${formatBytes(lastMem.used)} · ${fmtPct(memPct?.current, 0)}` : '—', tone: pctTone(memPct?.current) },
            { label: 'Available', value: lastMem?.available != null ? formatBytes(lastMem.available) : '—' },
            { label: 'Committed', value: lastMem?.committed ? formatBytes(lastMem.committed) : '—' },
            { label: 'Peak used', value: fmtPct(memPct?.peak), tone: pctTone(memPct?.peak) },
          ]}
        />
        <TimeSeries
          data={memRows}
          series={[
            { key: 'used', label: 'Used', color: C.violet },
            { key: 'cached', label: 'Cache', color: C.sky, line: true },
            { key: 'committed', label: 'Committed', color: C.orange, line: true, dashed: true },
            { key: 'total', label: 'Installed', color: C.slate, line: true, dashed: true },
          ]}
          height={240}
          yFormat={(v) => formatBytes(v)}
          tickFormat={tick}
        />
      </Panel>
      {hasSwap && (
        <Panel title="Page file" subtitle="Share of the page file in use">
          <TimeSeries
            data={swapRows}
            series={[{ key: 'swap', label: 'Page file used', color: C.orange }]}
            height={150}
            yFormat={(v) => `${v.toFixed(0)}%`}
            yDomain={[0, 100]}
            tickFormat={tick}
          />
        </Panel>
      )}

      {/* Disks */}
      <SectionTitle id="disks" icon={Gauge} title="Disks" hint="Physical disk activity" />
      {detail.disks.length ? (
        <>
          {diskNames.length > 1 && (
            <Segmented
              size="sm"
              items={diskNames.map((d) => ({ key: d, label: d }))}
              value={activeDisk.device}
              onChange={setDisk}
            />
          )}
          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="Throughput" subtitle={activeDisk.device}>
              <TimeSeries
                data={diskThroughput}
                series={[{ key: 'read', label: 'Read', color: C.teal }, { key: 'write', label: 'Write', color: C.orange }]}
                height={180}
                yFormat={(v) => `${formatBytes(v)}/s`}
                tickFormat={tick}
              />
            </Panel>
            <Panel title="Operations" subtitle="I/O per second">
              <TimeSeries
                data={diskIops}
                series={[{ key: 'read', label: 'Read IOPS', color: C.teal }, { key: 'write', label: 'Write IOPS', color: C.orange }]}
                height={180}
                yFormat={(v) => fmtNum(v)}
                tickFormat={tick}
              />
            </Panel>
            {hasLatency && (
              <Panel title="Latency" subtitle={`Average per operation · peak ${fmtMs(latencyStats?.peak)}`}>
                <TimeSeries
                  data={diskLatency}
                  series={[{ key: 'read', label: 'Read', color: C.teal, line: true }, { key: 'write', label: 'Write', color: C.orange, line: true }]}
                  height={180}
                  yFormat={(v) => fmtMs(v)}
                  tickFormat={tick}
                  references={[{ y: 20, label: '20 ms', tone: 'warning' }]}
                />
              </Panel>
            )}
            {hasBusy && (
              <Panel title="Busy time and queue" subtitle={`Utilisation peak ${fmtPct(utilStats?.peak, 0)}`}>
                <TimeSeries
                  data={diskUtil}
                  series={[{ key: 'util', label: 'Busy %', color: C.blue }, { key: 'queue', label: 'Queue', color: C.violet, line: true }]}
                  height={180}
                  yFormat={(v) => fmtNum(v)}
                  tickFormat={tick}
                />
              </Panel>
            )}
          </div>
          {(!hasLatency || !hasBusy) && (
            <p className="text-xs text-muted">
              {!hasLatency && !hasBusy ? 'Latency and busy time are' : !hasLatency ? 'Latency is' : 'Busy time is'} not
              reported by {latencyTrusted ? 'this agent' : 'the Windows agent yet'} for {activeDisk.device}.
            </p>
          )}
        </>
      ) : (
        <Panel><EmptyState icon={Gauge} title="No disk activity reported" compact /></Panel>
      )}

      {/* Volumes */}
      <SectionTitle id="volumes" icon={HardDrive} title="Volumes" hint="Usage over time, growth over the last week, and when each volume fills" />
      {detail.filesystems.length ? (
        <div className="grid gap-4 xl:grid-cols-5">
          <Panel title="Usage" subtitle="Share of each volume in use" className="xl:col-span-3">
            <TimeSeries
              data={volumeRows}
              series={detail.filesystems.map((fs, i) => ({ key: fs.mount, label: fs.mount, color: volumeColors[i % volumeColors.length], line: true }))}
              height={220}
              yFormat={(v) => `${v.toFixed(0)}%`}
              yDomain={[0, 100]}
              tickFormat={tick}
              references={[{ y: 90, label: '90%', tone: 'warning' }]}
            />
          </Panel>
          <Panel title="Capacity and forecast" className="xl:col-span-2" flush>
            <ul className="divide-y divide-border/60">
              {detail.filesystems.map((fs) => {
                const last = fs.points[fs.points.length - 1]
                const days = fs.days_until_full
                const growth = fs.growth_bytes_per_day
                return (
                  <li key={fs.mount} className="space-y-1.5 px-4 py-3">
                    <div className="flex items-baseline justify-between gap-2 text-sm">
                      <span className="font-medium">{fs.mount} <span className="text-xs font-normal text-muted">{fs.fs_type}</span></span>
                      <span className="tabular-nums text-text2">{fmtPct(last?.used_pct, 0)}</span>
                    </div>
                    <Meter pct={last?.used_pct} warn={85} crit={95} />
                    <div className="flex flex-wrap justify-between gap-2 text-xs text-muted">
                      <span>{formatBytes(last?.used_bytes ?? 0)} of {formatBytes(last?.total_bytes ?? 0)}</span>
                      <span>
                        {growth == null ? 'Growth: not enough data'
                          : growth <= 0 ? 'Not growing'
                            : `+${formatBytes(growth)}/day`}
                        {days != null && (
                          <span className={cn('ml-2 font-medium', days < 7 ? 'text-danger' : days < 30 ? 'text-warning' : 'text-text2')}>
                            full in ~{Math.max(1, Math.round(days))}d
                          </span>
                        )}
                      </span>
                    </div>
                  </li>
                )
              })}
            </ul>
          </Panel>
        </div>
      ) : (
        <Panel><EmptyState icon={HardDrive} title="No volumes reported" compact /></Panel>
      )}

      {/* Network */}
      <SectionTitle id="network" icon={NetworkIcon} title="Network" hint="Per interface" />
      {activeNic ? (
        <>
          {nicList.length > 1 && (
            <Segmented
              size="sm"
              items={nicList.map((i) => ({ key: i.name, label: i.name }))}
              value={activeNic.name}
              onChange={setNic}
            />
          )}
          <Panel title="Throughput" subtitle={activeNic.name}>
            <ChartStats
              items={[
                { label: 'Received now', value: formatBps(rxStats?.current ?? 0) },
                { label: 'Received peak', value: formatBps(rxStats?.peak ?? 0) },
                { label: 'Sent now', value: formatBps(txStats?.current ?? 0) },
                { label: 'Sent peak', value: formatBps(txStats?.peak ?? 0) },
              ]}
            />
            <TimeSeries
              data={nicThroughput}
              series={[{ key: 'rx', label: 'Received', color: C.sky }, { key: 'tx', label: 'Sent', color: C.green }]}
              height={220}
              yFormat={(v) => formatBps(v)}
              tickFormat={tick}
            />
          </Panel>
          {(hasPackets || errorTotal > 0) && (
            <div className={cn('grid gap-4', hasPackets && errorTotal > 0 && 'lg:grid-cols-2')}>
              {hasPackets && (
                <Panel title="Packets" subtitle="Per second">
                  <TimeSeries
                    data={nicPackets}
                    series={[{ key: 'rx', label: 'Received', color: C.sky, line: true }, { key: 'tx', label: 'Sent', color: C.green, line: true }]}
                    height={170}
                    yFormat={(v) => fmtNum(v)}
                    tickFormat={tick}
                  />
                </Panel>
              )}
              {errorTotal > 0 && (
                <Panel title="Errors and drops" subtitle="Per second · non-zero values point at cabling, drivers or congestion">
                  <TimeSeries
                    data={nicErrors}
                    series={[{ key: 'errors', label: 'Errors', color: C.red, line: true }, { key: 'drops', label: 'Drops', color: C.orange, line: true }]}
                    height={170}
                    yFormat={(v) => fmtNum(v, 2)}
                    tickFormat={tick}
                  />
                </Panel>
              )}
            </div>
          )}
          {errorTotal === 0 && (
            <div className="flex items-center gap-2 rounded-xl border border-border bg-surface px-4 py-3 text-sm text-text2">
              <CheckCircle2 className="h-4 w-4 text-success" />
              No errors or dropped packets on {activeNic.name} in this window.
              {!hasPackets && <span className="text-xs text-muted">Packet rates are not reported by this agent.</span>}
            </div>
          )}
        </>
      ) : (
        <Panel><EmptyState icon={NetworkIcon} title="No network interfaces reported" compact /></Panel>
      )}
    </div>
  )
}
