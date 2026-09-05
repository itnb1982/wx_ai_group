import sys
sys.stdout = open(r'F:\\WanxiangAI\test_mt5_dual.log', 'w')
sys.stderr = sys.stdout

import MetaTrader5 as mt5
import time

print('=== 1. 连接 liumanchun3 (STARTRADER 5) ===')
r3 = mt5.initialize(
    path=r'C:\Program Files\STARTRADER Financial MetaTrader 5\terminal64.exe',
    login=1610093301,
    password='Lmc20230717@',
    server='STARTRADERFinancial-Demo'
)
print(f'  r3={r3}, error={mt5.last_error()}')
if r3:
    info = mt5.account_info()
    print(f'  balance={info.balance if info else "N/A"}')

time.sleep(3)

print('\n=== 2. 连接 liumanchun4 (STARTRADER 52) ===')
r4 = mt5.initialize(
    path=r'C:\Program Files\STARTRADER Financial MetaTrader 52\terminal64.exe',
    login=1610098464,
    password='Lmc20230717@',
    server='STARTRADERFinancial-Demo'
)
print(f'  r4={r4}, error={mt5.last_error()}')
if r4:
    info = mt5.account_info()
    print(f'  balance={info.balance if info else "N/A"}')

print('\n=== 3. 当前运行的 terminal64 进程 ===')
import subprocess
result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq terminal64.exe'], capture_output=True, text=True)
print(result.stdout)

mt5.shutdown()
print('\nDone.')
sys.stdout.close()
