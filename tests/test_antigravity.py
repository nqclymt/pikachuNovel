import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from core import antigravity as agy
from core.cli_scheduler import FileLock, scheduler_slot, pause_scheduler
from core.llm_provider import LLMCallCancelled


FAKE_CLI = r'''
import json, os, pathlib, subprocess, sys, time
if '--version' in sys.argv:
    print('agy 2.3.4')
    sys.exit(0)
mode = os.getenv('FAKE_AGY_MODE', 'success')
if mode == 'auth':
    print('authentication required credential=SUPER-SECRET', file=sys.stderr)
    sys.exit(1)
if mode == 'quota':
    print(json.dumps({'event':'result', 'result':{'status':'ERROR', 'error':'RESOURCE_EXHAUSTED 429 secret-key'}}))
    sys.exit(1)
if mode == 'hang':
    time.sleep(30)
tools = ['run_command'] if mode == 'unsafe' else []
print(json.dumps({'event':'init', 'init':{'tools':tools}}), flush=True)
line = sys.stdin.readline()
if not line:
    sys.exit(4)
prompt = json.loads(line)['message']['content']
assert sys.stdin.read() == ''
capture = os.getenv('FAKE_AGY_CAPTURE')
if capture:
    agent = pathlib.Path('.agents/agents/pikachu-novel/agent.md').read_text(encoding='utf-8')
    pathlib.Path(capture).write_text(json.dumps({'prompt':prompt, 'argv':sys.argv[1:], 'cwd':os.getcwd(), 'agent':agent, 'api_key_present':'GEMINI_API_KEY' in os.environ}), encoding='utf-8')
if mode == 'stderr':
    sys.stderr.write('diagnostics-with-key' * 20000)
    sys.stderr.flush()
if mode == 'malformed':
    print('this is not NDJSON')
    sys.exit(0)
if mode == 'partial':
    print(json.dumps({'event':'step_update','step_update':{'step_type':'agent_response','text_delta':'半章正文'}}))
    sys.exit(0)
if mode == 'sleep':
    print(json.dumps({'event':'step_update','step_update':{'step_type':'agent_response','text_delta':'尚未完成'}}), flush=True)
    time.sleep(30)
if mode in ('child', 'orphan'):
    heartbeat = os.getenv('FAKE_AGY_HEARTBEAT')
    child_code = 'import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); [(p.write_text(str(time.time())),time.sleep(.05)) for _ in range(600)]'
    child = subprocess.Popen([sys.executable, '-c', child_code, heartbeat])
    pathlib.Path(heartbeat + '.pid').write_text(str(child.pid))
    if mode == 'orphan':
        sys.exit(0)
    time.sleep(30)
if mode == 'double':
    print(json.dumps({'event':'result', 'result':{'status':'SUCCESS', 'response':'第一份'}}))
result = {'status':'SUCCESS', 'response':'完整正文\n中文“对白”。'}
if mode == 'invalid_json':
    result['response'] = 'bad json'
if mode == 'json':
    result['structured_output'] = {'人物':'小明', '完整':True}
    result['response'] = 'ignored in favor of structured object'
if mode == 'error':
    result = {'status':'ERROR', 'error':'unknown model', 'response':'不可保存'}
print(json.dumps({'event':'result', 'result':result}, ensure_ascii=False), flush=True)
sys.exit(1 if mode == 'exit_nonzero' else 0)
'''


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('HARNESS_NOVEL_AGY_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.delenv('HARNESS_NOVEL_CANCEL_FILE', raising=False)
    monkeypatch.delenv('FAKE_AGY_MODE', raising=False)
    script = tmp_path / 'fake_agy.py'
    script.write_text(FAKE_CLI, encoding='utf-8')
    capture = tmp_path / 'capture.json'
    monkeypatch.setenv('FAKE_AGY_CAPTURE', str(capture))
    monkeypatch.setattr(agy, '_resolve_cli', lambda _: sys.executable)
    original = agy._run_process

    def fake_process(argv, *args):
        return original([argv[0], '-X', 'utf8', str(script)] + argv[1:], *args)

    monkeypatch.setattr(agy, '_run_process', fake_process)
    return capture


def test_long_chinese_prompt_uses_stdin_and_isolated_output_agent(isolated, monkeypatch):
    prompt = ('第一行中文\n第二行包含 %PATH% & `命令` $(表达式) "引号"\n' * 3000)
    monkeypatch.setenv('GEMINI_API_KEY', 'must-not-pass')
    text = agy.run_antigravity(prompt, model='custom-model', timeout=5)
    capture = json.loads(isolated.read_text(encoding='utf-8'))
    assert text.startswith('完整正文')
    assert capture['prompt'] == prompt
    assert prompt not in ' '.join(capture['argv'])
    assert '-p' not in capture['argv']
    assert '--continue' not in capture['argv']
    assert '--dangerously-skip-permissions' not in capture['argv']
    assert capture['api_key_present'] is False
    assert not Path(capture['cwd']).exists()
    assert 'tools: []' in capture['agent']
    assert agy.scheduler_status()['running'] is False


@pytest.mark.parametrize('mode,category', [('partial','protocol'), ('malformed','protocol'), ('double','protocol'), ('error','process'), ('exit_nonzero','process')])
def test_rejects_incomplete_invalid_and_failed_outputs(isolated, monkeypatch, mode, category):
    monkeypatch.setenv('FAKE_AGY_MODE', mode)
    with pytest.raises(agy.AntigravityError) as error:
        agy.run_antigravity('测试', timeout=5)
    assert error.value.category == category
    assert not agy.scheduler_status()['running']


def test_stderr_is_drained_without_leaking_credentials(isolated, monkeypatch):
    monkeypatch.setenv('FAKE_AGY_MODE', 'stderr')
    assert agy.run_antigravity('测试', timeout=5).startswith('完整正文')


@pytest.mark.parametrize('mode', ['auth', 'quota'])
def test_credentials_and_quota_pause_all_future_calls_until_resumed(isolated, monkeypatch, mode):
    monkeypatch.setenv('FAKE_AGY_MODE', mode)
    with pytest.raises(agy.AntigravityError) as error:
        agy.run_antigravity('测试', timeout=5)
    assert error.value.category == mode
    assert 'secret' not in str(error.value).lower()
    status = agy.scheduler_status()
    assert status['paused'] and status['category'] == mode
    monkeypatch.setenv('FAKE_AGY_MODE', 'success')
    with pytest.raises(agy.AntigravityError) as paused:
        agy.run_antigravity('不应提交', timeout=5)
    assert paused.value.category == mode
    assert not isolated.exists()
    assert not agy.resume_scheduler()['paused']
    assert agy.run_antigravity('恢复后提交', timeout=5).startswith('完整正文')


def test_structured_output_and_schema(isolated, monkeypatch):
    monkeypatch.setenv('FAKE_AGY_MODE', 'json')
    response = agy.run_antigravity('结构化', is_json=True, timeout=5)
    assert json.loads(response) == {'人物':'小明', '完整':True}
    assert '--json-schema' in json.loads(isolated.read_text())['argv']
    monkeypatch.setenv('FAKE_AGY_MODE', 'invalid_json')
    with pytest.raises(agy.AntigravityError, match='JSON'):
        agy.run_antigravity('结构化', is_json=True, timeout=5)


def test_agent_tools_rejected_before_prompt_sent(isolated, monkeypatch):
    monkeypatch.setenv('FAKE_AGY_MODE', 'unsafe')
    with pytest.raises(agy.AntigravityError, match='tools'):
        agy.run_antigravity('私密正文', cli_agent='external-agent', timeout=5)
    assert not isolated.exists()


def test_timeout_terminates_process_and_releases_slot(isolated, monkeypatch):
    monkeypatch.setenv('FAKE_AGY_MODE', 'hang')
    start = time.monotonic()
    with pytest.raises(agy.AntigravityError) as error:
        agy.run_antigravity('测试', timeout=0.3)
    assert error.value.category == 'timeout'
    assert time.monotonic() - start < 5
    assert not agy.scheduler_status()['running']


def test_cancel_active_call_discards_partial_response(isolated, monkeypatch):
    monkeypatch.setenv('FAKE_AGY_MODE', 'sleep')
    cancel = threading.Event()
    timer = threading.Timer(0.4, cancel.set)
    timer.start()
    try:
        with pytest.raises(LLMCallCancelled):
            agy.run_antigravity('测试', timeout=5, cancel_event=cancel)
    finally:
        timer.cancel()
    assert not agy.scheduler_status()['running']


def test_cancel_marker_prevents_launch(isolated, monkeypatch, tmp_path):
    marker = tmp_path / 'cancel'
    marker.touch()
    monkeypatch.setenv('HARNESS_NOVEL_CANCEL_FILE', str(marker))
    with pytest.raises(LLMCallCancelled):
        agy.run_antigravity('测试')
    assert not isolated.exists()


@pytest.mark.parametrize('mode', ['child', 'orphan'])
def test_timeout_terminates_cli_descendants(isolated, monkeypatch, tmp_path, mode):
    monkeypatch.setenv('FAKE_AGY_MODE', mode)
    heartbeat = tmp_path / 'heartbeat'
    monkeypatch.setenv('FAKE_AGY_HEARTBEAT', str(heartbeat))
    try:
        with pytest.raises(agy.AntigravityError) as error:
            agy.run_antigravity('测试', timeout=1)
        assert error.value.category == 'timeout'
        assert heartbeat.exists()
        before = heartbeat.read_text()
        time.sleep(0.2)
        assert heartbeat.read_text() == before
    finally:
        # Contain a regression in the test itself instead of leaving a background child.
        pid_file = Path(str(heartbeat) + '.pid')
        if pid_file.exists() and os.name == 'nt':
            subprocess.run(['taskkill', '/PID', pid_file.read_text(), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, check=False)


def test_queued_call_is_cancelable_without_launch(isolated):
    cancel = threading.Event()
    with scheduler_slot():
        timer = threading.Timer(0.3, cancel.set)
        timer.start()
        try:
            with pytest.raises(LLMCallCancelled):
                agy.run_antigravity('不应提交', timeout=5, cancel_event=cancel)
        finally:
            timer.cancel()
    assert not isolated.exists()
    assert agy.scheduler_status()['waiting'] == 0


def test_threads_serialize_and_queue_is_visible(isolated):
    seen = []
    errors = []
    def worker():
        try:
            with scheduler_slot():
                seen.append('started')
        except Exception as error:
            errors.append(error)
    with scheduler_slot():
        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.monotonic() + 3
        while agy.scheduler_status()['waiting'] == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert agy.scheduler_status()['waiting'] == 1
        assert not seen
    thread.join(timeout=3)
    assert seen == ['started'] and not errors


def test_cross_process_lock_and_crash_release(isolated, tmp_path):
    signal_file = tmp_path / 'acquired'
    code = ('from core.cli_scheduler import scheduler_slot; import pathlib,time; '
            'slot=scheduler_slot(); slot.__enter__(); '
            'pathlib.Path(__import__("sys").argv[1]).write_text("yes"); time.sleep(30)')
    proc = subprocess.Popen([sys.executable, '-c', code, str(signal_file)], cwd=str(Path(__file__).resolve().parents[1]))
    try:
        deadline = time.monotonic() + 5
        while not signal_file.exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        assert signal_file.exists()
        assert agy.scheduler_status()['running']
    finally:
        proc.kill()
        proc.wait(timeout=5)
    with scheduler_slot():
        assert agy.scheduler_status()['running']
    assert not agy.scheduler_status()['running']


def test_paused_state_shared_with_other_process(isolated):
    pause_scheduler('quota', '请恢复')
    code = 'from core.antigravity import scheduler_status; import json; print(json.dumps(scheduler_status()))'
    output = subprocess.check_output([sys.executable, '-c', code], cwd=str(Path(__file__).resolve().parents[1]))
    assert json.loads(output)['category'] == 'quota'


def test_probe_is_version_only(monkeypatch, tmp_path):
    monkeypatch.setattr(agy, '_resolve_cli', lambda _: sys.executable)
    result = agy.probe_antigravity()
    assert result['installed']
    assert result['version'].startswith('3.')
    assert not result['error']


def test_missing_binary_and_shell_wrapper_rejected(tmp_path):
    result = agy.probe_antigravity(str(tmp_path / 'missing-agy'))
    assert not result['installed'] and result['error']
    if os.name == 'nt':
        wrapper = tmp_path / 'agy.cmd'
        wrapper.write_text('@echo no')
        with pytest.raises(agy.AntigravityError, match='包装脚本'):
            agy._resolve_cli(str(wrapper))
