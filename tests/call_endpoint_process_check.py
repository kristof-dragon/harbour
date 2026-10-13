"""Run inside the built endpoint image with networking disabled.

Mount a test endpoint.json at /run/endpoint.json and provide writable /logs and
/tmp. Override the entrypoint with Python and run this file. Starts and stops
only this disposable container's service; no remote host is contacted.
"""
import concurrent.futures,datetime,json,os,signal,subprocess,sys,time,urllib.request,urllib.error,threading
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
config=json.loads(Path('/run/endpoint.json').read_text())
callback_received=threading.Event()
class Callback(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        assert self.headers['Authorization']=='Bearer '+config['callback_token']
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert len(data['boot_id'])==32 and data['protocol']==1
        callback_received.set();self.send_response(200);self.send_header('Content-Length','0');self.end_headers()
callback_server=HTTPServer(('127.0.0.1',18081),Callback)
threading.Thread(target=callback_server.serve_forever,daemon=True).start()
service=subprocess.Popen([sys.executable,'-I','/app/service.py'])
def fetch(path,token=config['token']):
    req=urllib.request.Request('http://127.0.0.1:8080'+path,headers={'Authorization':'Bearer '+token})
    with urllib.request.urlopen(req,timeout=5) as r:return r.read()
try:
    for _ in range(100):
        try:fetch('/health');break
        except Exception:time.sleep(.1)
    else:raise AssertionError('No healthy endpoint')
    try:fetch('/logs','invalid');raise AssertionError('Unauthorized request succeeded')
    except urllib.error.HTTPError as exc:assert exc.code==401
    assert callback_received.wait(5),'No startup callback'
    status=json.loads(fetch('/status'));assert status['max_duration']==3600 and status['ready']
    test_id='a'*32
    def run(direction,rate,port):
        command=['iperf3','-c','127.0.0.1','-p',str(port),'-u','-b',rate,'-l','1200','-t','10','-i','1','-J','--get-server-output','--extra-data','harbour:'+test_id+':'+direction]
        if direction=='download':command.append('-R')
        result=subprocess.run(command,capture_output=True,timeout=25)
        assert result.returncode==0,result.stderr
        data=json.loads(result.stdout);assert 'error' not in data,data
        assert data['end']['sum_received']['packets']>0
        if direction=='upload':assert len(data['server_output_json']['intervals'])>=9
        return direction,data
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda args:run(*args),[('upload','128K',5201),('download','256K',5202)]))
    time.sleep(.2)
    remote=json.loads(fetch('/runs/'+test_id))['records'];assert len(remote)==2,remote
    for direction,local in results:
        record=next(r for r in remote if r['direction']==direction)
        assert record['raw']['extra_data']==local['extra_data']
        assert record['raw']['start']['cookie']==local['start']['cookie']
    # Negotiate a full-hour run and stop it early; this verifies the deployed
    # server limit without manufacturing an hour of network observations.
    long_run=subprocess.Popen(['iperf3','-c','127.0.0.1','-p','5201','-u','-b','64K','-t','3600','-i','1','-J'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    time.sleep(3);assert long_run.poll() is None,'Hour-long run was rejected'
    long_run.send_signal(signal.SIGINT);long_output,_=long_run.communicate(timeout=10)
    assert json.loads(long_output)['start']['test_start']['duration']==3600
    day=datetime.datetime.now(datetime.timezone.utc).strftime('%y-%m-%d')
    assert day in json.loads(fetch('/logs'))['days']
    files=json.loads(fetch('/logs/'+day))['files'];assert len(files)>=2
    for source in ['upload','download']:
        name=next(x['name'] for x in files if x['name'].startswith(source))
        records=[json.loads(line) for line in fetch('/logs/'+day+'/'+name).splitlines()]
        output=''.join(x.get('text','') for x in records if x['event']=='iperf_output')
        assert 'sum_received' in output,(source,output)
        assert config['token'] not in output
    for bad in ['/logs/../endpoint.json','/logs/'+day+'/../../endpoint.json']:
        try:fetch(bad);raise AssertionError('Unsafe path accepted')
        except urllib.error.HTTPError as exc:assert exc.code==404
    print('PASS: real simultaneous UDP upload/download, receiver intervals, authenticated HTTP, daily local logs, unsafe paths;',[(d,r['end']['sum_received']['lost_percent']) for d,r in results])
finally:
    callback_server.shutdown();callback_server.server_close()
    service.send_signal(signal.SIGTERM)
    service.wait(timeout=12)
    assert service.returncode==0,service.returncode
