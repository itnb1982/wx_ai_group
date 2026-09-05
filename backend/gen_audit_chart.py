import pandas as pd
from datetime import datetime

df = pd.read_csv('F:/WanxiangAI/backend/audit_20260814_full_prices.csv', parse_dates=['time_utc','time_server'])
mask = (df['time_utc'] >= '2026-08-14 00:00:00') & (df['time_utc'] <= '2026-08-14 12:00:00')
df = df[mask].copy().reset_index(drop=True)

W, H = 900, 420
pad_l, pad_r, pad_t, pad_b = 70, 40, 40, 60

t_min = df['time_utc'].min().timestamp()
t_max = df['time_utc'].max().timestamp()
p_min = df['low'].min() - 5
p_max = df['high'].max() + 5

def x_of(t):
    return pad_l + (t - t_min) / (t_max - t_min) * (W - pad_l - pad_r)
def y_of(p):
    return H - pad_b - (p - p_min) / (p_max - p_min) * (H - pad_t - pad_b)

path_pts = []
for _, r in df.iterrows():
    path_pts.append(f"{x_of(r['time_utc'].timestamp())},{y_of(r['close'])}")
close_path = 'M' + ' L'.join(path_pts)

area_pts = []
for _, r in df.iterrows():
    area_pts.append(f"{x_of(r['time_utc'].timestamp())},{y_of(r['low'])}")
for _, r in df.iterrows():
    area_pts.append(f"{x_of(r['time_utc'].timestamp())},{y_of(r['high'])}")
area_path = 'M' + ' L'.join(area_pts) + ' Z'

trades = [
    ('BUY #1',  '2026-08-14 00:15:00', 4359.30),
    ('SELL #2', '2026-08-14 03:35:00', 4315.29),
    ('SELL #3', '2026-08-14 06:14:00', 4322.11),
]

lines = []
lines.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
lines.append('  <rect width="%d" height="%d" fill="#fafafa"/>' % (W,H))
lines.append(f'  <text x="{W/2}" y="25" text-anchor="middle" font-size="16" font-weight="bold" fill="#333">XAUUSD 2026-08-14 亚盘行情与三单开仓位置</text>')
lines.append(f'  <text x="{W/2}" y="42" text-anchor="middle" font-size="11" fill="#666">红线=亏损单 | 下轴=UTC | 上轴=MT5 Server Time (UTC+3)</text>')
lines.append('  <g stroke="#e0e0e0" stroke-width="1">')

for p in range(int(p_min)+1, int(p_max), 5):
    y = y_of(p)
    lines.append(f'    <line x1="{pad_l}" y1="{y}" x2="{W-pad_r}" y2="{y}"/>')
for h in range(0, 13, 2):
    t = datetime(2026,8,14,h,0).timestamp()
    x = x_of(t)
    lines.append(f'    <line x1="{x}" y1="{pad_t}" x2="{x}" y2="{H-pad_b}"/>')
lines.append('  </g>')
lines.append(f'  <path d="{area_path}" fill="#1f77b4" opacity="0.12"/>')
lines.append(f'  <path d="{close_path}" fill="none" stroke="#1f77b4" stroke-width="1.5"/>')

for name, t_str, price in trades:
    t = pd.to_datetime(t_str).timestamp()
    x = x_of(t)
    y = y_of(price)
    color = '#d62728'
    lines.append(f'  <line x1="{x}" y1="{pad_t}" x2="{x}" y2="{H-pad_b}" stroke="{color}" stroke-width="1" stroke-dasharray="4,3" opacity="0.7"/>')
    lines.append(f'  <circle cx="{x}" cy="{y}" r="5" fill="{color}"/>')
    lines.append(f'  <text x="{x+8}" y="{y-8}" font-size="11" fill="{color}" font-weight="bold">{name} @{price:.2f}</text>')

lines.append(f'  <text x="{pad_l-10}" y="{H-pad_b+15}" text-anchor="end" font-size="10" fill="#555">{int(p_min)}</text>')
lines.append(f'  <text x="{pad_l-10}" y="{pad_t+5}" text-anchor="end" font-size="10" fill="#555">{int(p_max)}</text>')
for p in range(int(p_min)+5, int(p_max), 10):
    y = y_of(p)
    lines.append(f'  <text x="{pad_l-8}" y="{y+3}" text-anchor="end" font-size="9" fill="#777">{p}</text>')

for h in range(0, 13, 2):
    t = datetime(2026,8,14,h,0).timestamp()
    x = x_of(t)
    lines.append(f'  <text x="{x}" y="{H-pad_b+18}" text-anchor="middle" font-size="9" fill="#555">{h:02d}:00</text>')
for h in [3,6,9,12]:
    t = datetime(2026,8,14,h-3,0).timestamp()
    x = x_of(t)
    lines.append(f'  <text x="{x}" y="{pad_t-6}" text-anchor="middle" font-size="9" fill="#555">{h:02d}:00</text>')

lines.append('</svg>')

with open('F:/WanxiangAI/backend/audit_20260814_chart.svg','w',encoding='utf-8') as f:
    f.write('\n'.join(lines))
print('saved SVG')
