import sys
sys.stdout = open(r'F:\\WanxiangAI\test_liumanchun4_after.log', 'w')

import MetaTrader5 as mt5
import time
import subprocess

print('=== liumanchun4 连接测试（清理后）===')

# 先检查当前进程
r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq terminal64.exe'], capture_output=True, text=True)
print(f'当前 terminal64 进程:\n{r.stdout}')

print('\n--- 调用 mt5.initialize ---')
start = time.time()
r = mt5.initialize(
    path=r'C:\Program Files\STARTRADER Financial MetaTrader 52\terminal64.exe',
    login=1610098464,
    password='Lmc20230717@',
    server='STARTRADERFinancial-Demo'
)
elapsed = time.time() - start
print(f'initialize={r} (耗时 {elapsed:.1f}s)')
print(f'last_error={mt5.last_error()}')

if r:
    info = mt5.account_info()
    if info:
        print(f'✅ 成功! login={info.login} balance={info.balance} equity={info.equity}')
    else:
        print('❌ account_info 返回 None')
    mt5.shutdown()
else:
    print('❌ initialize 失败')

print('\nDone.')
sys.stdout.close()
