# UI/Dashboard Design

## Design System

### Color Palette (Dark Mode Default)

```
Background:
  --bg-primary:    #0F1117    (main background)
  --bg-secondary:  #1A1D27    (cards, panels)
  --bg-tertiary:   #242832    (inputs, hover states)
  --bg-elevated:   #2D3140    (dropdowns, modals)

Text:
  --text-primary:  #E8EAED    (headings, important text)
  --text-secondary:#9BA1B0    (body text, labels)
  --text-muted:    #5F6578    (placeholders, disabled)

Status Colors:
  --status-up:      #22C55E   (green - healthy/online)
  --status-down:    #EF4444   (red - critical/offline)
  --status-degraded:#EAB308   (yellow - warning/degraded)
  --status-unknown: #6B7280   (gray - unknown)
  --status-maint:   #3B82F6   (blue - maintenance)

Accent:
  --accent:         #6366F1   (indigo - primary actions)
  --accent-hover:   #818CF8   (lighter indigo)

Charts:
  --chart-1:        #6366F1   (indigo)
  --chart-2:        #22C55E   (green)
  --chart-3:        #F59E0B   (amber)
  --chart-4:        #EF4444   (red)
  --chart-5:        #8B5CF6   (violet)
  --chart-6:        #06B6D4   (cyan)
```

### Typography
- **Font**: Inter (UI), JetBrains Mono (metrics/code)
- **Headings**: 600 weight, --text-primary
- **Body**: 400 weight, --text-secondary
- **Metrics/Numbers**: JetBrains Mono 500 weight

### Spacing
- Base unit: 4px
- Card padding: 16px (4 units)
- Section gap: 24px (6 units)
- Page margin: 32px (8 units)

---

## Page Layouts

### 1. Main Dashboard (/)

```
┌─────────────────────────────────────────────────────────────────┐
│ [Logo] ZenPlus          🔍 Search...    🔔 3  👤 Admin  ☀/🌙 │
├────────┬────────────────────────────────────────────────────────┤
│        │                                                        │
│  📊    │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ │
│ Dash   │  │ Total    │ │ Online   │ │ Degraded │ │ Offline  │ │
│        │  │  1,247   │ │  1,198   │ │    31    │ │    18    │ │
│  🖥️    │  │          │ │  96.1%   │ │   2.5%   │ │   1.4%   │ │
│ Devices│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ │
│        │                                                        │
│  🗺️    │  ┌─────────────────────────┐ ┌────────────────────────┤
│ Topo   │  │                         │ │  Active Alerts         │
│        │  │   Response Time Graph   │ │  ┌────────────────────┐│
│  🔔    │  │   (Last 24 hours)       │ │  │🔴 router-gw down  ││
│ Alerts │  │   ~~~~~~~~~~~~~~~~~~    │ │  │   2 min ago        ││
│        │  │                         │ │  │🟡 sw-floor3 high   ││
│  ⚙️    │  │                         │ │  │   RTT (45ms)       ││
│ Settings│  └─────────────────────────┘ │  │🔴 fw-dmz timeout  ││
│        │                               │  │   5 min ago        ││
│        │  ┌─────────────────────────┐  │  └────────────────────┘│
│        │  │  Device Status Heatmap  │  │                        │
│        │  │  ■■■■■■■■■■■■■■■■■■■■  │  │  Recent Status Changes│
│        │  │  ■■■■■■■■■■■■■■■■■■■■  │  │  router-01: up→down  │
│        │  │  ■■■■■■■■■■■■■■■■■■■■  │  │  switch-05: down→up  │
│        │  └─────────────────────────┘  │  ap-lobby: up→degraded│
│        │                               │                        │
└────────┴───────────────────────────────┴────────────────────────┘
```

**Widgets:**
- **KPI Cards** (top): Total devices, Online %, Degraded, Offline - real-time counters with trend arrows
- **Response Time Graph**: ECharts line chart, multi-device overlay, zoomable time axis
- **Active Alerts Panel**: Scrollable list, color-coded severity, click to acknowledge
- **Device Status Heatmap**: Color-coded grid showing all devices at a glance
- **Recent Status Changes**: Live feed of up/down transitions

### 2. Devices Page (/devices)

```
┌─────────────────────────────────────────────────────────────────┐
│ Devices                              [+ Add Device] [Import]    │
├─────────────────────────────────────────────────────────────────┤
│ 🔍 Search...  │ Status: [All ▼] │ Group: [All ▼] │ Type: [▼]  │
├─────────────────────────────────────────────────────────────────┤
│ ●  Hostname       IP Address      Type     RTT      Status     │
│ 🟢 router-core-01 10.0.0.1       Router   2.1ms    Online     │
│ 🟢 switch-floor1  10.0.1.1       Switch   1.3ms    Online     │
│ 🔴 firewall-dmz   10.0.0.5       Firewall --       Offline    │
│ 🟡 ap-lobby-01    10.0.2.10      AP       45.2ms   Degraded   │
│ 🟢 server-web-01  10.0.10.1      Server   0.8ms    Online     │
│ ...                                                             │
├─────────────────────────────────────────────────────────────────┤
│ Showing 1-50 of 1,247              [< 1 2 3 ... 25 >]         │
└─────────────────────────────────────────────────────────────────┘
```

**Features:**
- Sortable columns (click header)
- Multi-select for bulk actions (delete, change group, enable/disable)
- Inline status indicator with color + icon
- Quick filter chips for status
- CSV/JSON export
- Click row to open device detail

### 3. Device Detail (/devices/:id)

```
┌─────────────────────────────────────────────────────────────────┐
│ ← Back   router-core-01                        [Edit] [Delete] │
│          10.0.0.1 | Router | Core Network       🟢 Online      │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐      │
│  │ Avg RTT   │ │ Max RTT   │ │ Pkt Loss  │ │ Uptime    │      │
│  │  2.1 ms   │ │  8.4 ms   │ │   0.0%    │ │ 99.97%    │      │
│  │  ↓ 0.3ms  │ │  ↓ 1.2ms  │ │  = 0%     │ │ (30 days) │      │
│  └───────────┘ └───────────┘ └───────────┘ └───────────┘      │
│                                                                 │
│  [1h] [6h] [24h] [7d] [30d] [Custom]                          │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │           Response Time & Packet Loss                    │   │
│  │   8ms ┤                                                  │   │
│  │   6ms ┤        ╱╲                                        │   │
│  │   4ms ┤  ──╱──╱  ╲──╱──────────╱╲──────                │   │
│  │   2ms ┤──╱                        ╲──────               │   │
│  │   0ms ┤                                                  │   │
│  │       └──────────────────────────────────────────────    │   │
│  │        00:00   04:00   08:00   12:00   16:00   20:00    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  ┌──────────────────────────┐  ┌──────────────────────────┐   │
│  │ Status Timeline          │  │ Alert History            │   │
│  │ ████████████░░████████   │  │ No alerts in last 24h    │   │
│  │ (green=up, red=down)     │  │                          │   │
│  └──────────────────────────┘  └──────────────────────────┘   │
│                                                                 │
│  Device Information                                            │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ IP Address:  10.0.0.1     │ Type:     Router            │   │
│  │ Location:    Server Room  │ Group:    Core Network       │   │
│  │ Ping Interval: 60s       │ SNMP:     Disabled           │   │
│  │ Added:   2026-03-15      │ Last Seen: 2 seconds ago     │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 4. Network Topology (/topology)

```
┌─────────────────────────────────────────────────────────────────┐
│ Network Topology            [Auto Layout] [Export] [Fullscreen] │
├─────────────────────────────────────────────────────────────────┤
│  Zoom: [- ═══●═══ +]  │  Filter: [All ▼]  │  Layout: [Tree ▼] │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│                     ┌──────────┐                                │
│                     │ Internet │                                │
│                     │    🌐    │                                │
│                     └────┬─────┘                                │
│                          │                                      │
│                   ┌──────┴──────┐                               │
│                   │  Firewall   │                               │
│                   │  🟢 1.2ms  │                               │
│                   └──┬──────┬──┘                                │
│              ┌───────┘      └───────┐                           │
│       ┌──────┴──────┐       ┌──────┴──────┐                    │
│       │  Core SW 1  │       │  Core SW 2  │                    │
│       │  🟢 0.8ms  │       │  🟡 12ms   │                    │
│       └──┬──────┬──┘       └──┬──────┬──┘                     │
│          │      │             │      │                          │
│     [Floor1] [Floor2]   [Floor3] [Server]                      │
│                                                                 │
│  Legend: 🟢 Online  🟡 Degraded  🔴 Offline  ⚪ Unknown       │
└─────────────────────────────────────────────────────────────────┘
```

**Features:**
- Cytoscape.js with Cola/COSE layout engine
- Nodes colored by status (real-time updates)
- Click node for quick stats popup
- Double-click to navigate to device detail
- Drag to rearrange, mouse wheel to zoom
- Auto-layout algorithms (hierarchical, force-directed, circular)

### 5. Alerts Page (/alerts)

```
┌─────────────────────────────────────────────────────────────────┐
│ Alerts (18 active)                         [+ Create Rule]      │
├─────────────────────────────────────────────────────────────────┤
│ [Active 18] [Acknowledged 5] [Resolved] [Rules]               │
├─────────────────────────────────────────────────────────────────┤
│ 🔴 CRITICAL  firewall-dmz is unreachable           2 min ago   │
│              No response for 5 consecutive checks               │
│              [Acknowledge] [Resolve]                            │
│ ─────────────────────────────────────────────────────────────── │
│ 🟡 WARNING   ap-lobby-01 high latency (45ms)      10 min ago  │
│              RTT exceeded 30ms threshold                        │
│              [Acknowledge] [Resolve]                            │
│ ─────────────────────────────────────────────────────────────── │
│ 🔴 CRITICAL  switch-floor3 is unreachable          15 min ago  │
│              No response for 5 consecutive checks               │
│              [Acknowledge] [Resolve]                            │
└─────────────────────────────────────────────────────────────────┘
```

---

## Responsive Breakpoints

| Breakpoint | Width | Layout |
|-----------|-------|--------|
| Desktop | >1280px | Full sidebar + multi-column |
| Tablet | 768-1280px | Collapsed sidebar + 2 columns |
| Mobile | <768px | Bottom nav + single column |

## Animations & Transitions
- Status changes: Smooth color fade (300ms ease)
- Alert appearance: Slide-in from right (200ms)
- Chart updates: Animated data point transitions
- Page transitions: Fade (150ms)
- Hover states: 150ms ease-in-out

## Accessibility
- WCAG 2.1 AA compliance
- All status indicators use color + icon + text (never color alone)
- Keyboard navigation for all interactive elements
- aria-labels on all status indicators
- Focus ring visible on tab navigation
- Minimum touch target: 44x44px on mobile
