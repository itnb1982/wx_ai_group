import sys, os, json, time, traceback

# 模拟 mt5_worker.py 的行为，但输出更详细的日志
log_path = r'F:\\WanxiangAI\test_liumanchun4_diag.log'
with open(log_path, 'w') as f:
    def log(msg):
        f.write(msg + '\n')
        f.flush()
    
    try:
        log('=== liumanchun4 诊断开始 ===')
        log(f'PID={os.getpid()}')
        log(f'Python={sys.executable}')
        
        log('\n--- 1. 导入 MetaTrader5 ---')
        import MetaTrader5 as mt5
        log(f'MT5 version={mt5.__version__}')
        
        path = r'C:\Program Files\STARTRADER Financial MetaTrader 52\terminal64.exe'
        login = 1610098464
        password = 'Lmc20230717@'
        server = 'STARTRADERFinancial-Demo'
        
        log(f'\n--- 2. 调用 mt5.initialize ---')
        log(f'path={path}')
        log(f'login={login}')
        log(f'server={server}')
        
        # 先检查当前 terminal64 进程数
        import subprocess
        result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq terminal64.exe'], capture_output=True, text=True)
        log(f'\n当前 terminal64 进程:\n{result.stdout}')
        
        start = time.time()
        r = mt5.initialize(path=path, login=login, password=password, server=server)
        elapsed = time.time() - start
        log(f'\ninitialize 返回: {r} (耗时 {elapsed:.1f}s)')
        log(f'last_error: {mt5.last_error()}')
        
        if r:
            log('\n--- 3. 查询 account_info ---')
            info = mt5.account_info()
            if info:
                log(f'login={info.login} balance={info.balance} equity={info.equity}')
            else:
                log('account_info() 返回 None')
            
            log('\n--- 4. 再次检查 terminal64 进程 ---')
            result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq terminal64.exe'], capture_output=True, text=True)
            log(result.stdout)
        else:
            log('\ninitialize 失败，尝试不带 login 参数再试一次...')
            mt5.shutdown()
            time.sleep(1)
            start = time.time()
            r2 = mt5.initialize(path=path)
            elapsed = time.time() - start
            log(f'initialize(无login) 返回: {r2} (耗时 {elapsed:.1f}s)')
            log(f'last_error: {mt5.last_error()}')
            
            if r2:
                info = mt5.account_info()
                log(f'account_info: {info}')
            mt5.shutdown()
        
        log('\n=== 诊断结束 ===')
    except Exception as e:
        log(f'\n!!! 异常: {e}')
        traceback.print_exc(file=f)
