import sys
import threading

import pytest

from test_harbour import client, wait_job
from harbour import app as module, remote_probe, store


def render(chunks):
    output = remote_probe.TerminalOutput()
    for chunk in chunks:
        output.feed(chunk)
    return output.text()


@pytest.mark.parametrize('split', [1, 2, 7, 4096])
def test_docker_layers_rewrite_independently_across_chunks(split):
    # Docker's jsonmessage writer allocates rows, moves up, clears each row,
    # writes a layer and returns to the row below the progress block.
    chunks = '\n\x1b[1A\x1b[2K\raaaa: Downloading 1MB/8MB\r\x1b[1B'
    chunks += '\n\x1b[1A\x1b[2K\rbbbb: Waiting\r\x1b[1B'
    for i in range(100):
        chunks += f'\x1b[2A\x1b[2K\raaaa: Downloading {i}%\r\x1b[2B'
    chunks += '\x1b[2A\x1b[2K\raaaa: Pull complete\r\x1b[2B'
    chunks += '\x1b[1A\x1b[2K\rbbbb: Pull complete\r\x1b[1B'
    chunks += 'Digest: sha256:123\r\n'
    assert render(chunks[i:i+split] for i in range(0, len(chunks), split)) == (
        'aaaa: Pull complete\nbbbb: Pull complete\nDigest: sha256:123\n')


def test_compose_redraw_shorter_rows_and_clear_remaining_screen():
    frames = ['[+] Running 0/2\r\n ⠋ Container web  Recreating\r\n ⠋ Container db  Starting\r\n']
    frames += ['\x1b[3A\r\x1b[2K[+] Running 2/2\r\n\x1b[2K ✔ Container web  Started\r\n',
               '\x1b[2K ✔ Container db  Healthy\r\n\x1b[0J']
    assert render(frames) == '[+] Running 2/2\n ✔ Container web  Started\n ✔ Container db  Healthy\n'
    assert render(['Downloading 100%', '\rDone', '\x1b[K']) == 'Done'
    assert render(['abc\bD\tX']) == 'abD     X'


def test_escape_strings_colours_and_untrusted_text_are_plain_output():
    raw = '\x1b[?25l\x1b[32m✔ Started\x1b[0m\x1b[?25h\r\n'
    raw += '\x1b]0;hidden title\x07\x1b]8;;https://invalid.example\x1b\\link\x1b]8;;\x1b\\'
    raw += '\x1bPdiscarded control payload\x1b\\<script>bad()</script>'
    assert render(raw) == '✔ Started\nlink<script>bad()</script>'


def test_scrollback_is_bounded_without_losing_active_rows():
    output = remote_probe.TerminalOutput()
    for _ in range(2000):
        output.feed('x' * 100 + '\r\n')
    output.feed('layer: Downloading\r\n\x1b[A\r\x1b[2Klayer: Complete\r\n')
    assert len(output.text()) <= 60000
    assert output.text().endswith('layer: Complete\n')
    assert 'Downloading' not in output.text()
    # Invalid/huge cursor requests must not allocate unbounded screen space.
    output.feed('\x1b[999999999999999999B\x1b[999999999999999999Cdone')
    assert len(output.lines) <= output.max_rows
    assert max(map(len, output.lines)) <= output.max_columns


def test_process_has_terminal_output_noninteractive_input_and_final_render(tmp_path):
    acknowledged = tmp_path / 'ack'
    events = []
    def emit(event):
        events.append(event)
        if 'Downloading' in event.get('text', ''):
            acknowledged.touch()
    script = (
        'import os,sys,time,pathlib\n'
        'assert os.isatty(1) and os.isatty(2) and not os.isatty(0)\n'
        'assert os.get_terminal_size(1).columns == 160\n'
        'assert sys.stdin.read() == ""\n'
        'os.write(1,b"layer: Downloading\\n")\n'
        f'while not pathlib.Path({str(acknowledged)!r}).exists(): time.sleep(.01)\n'
        'os.write(2,b"\\x1b[A\\r\\x1b[2Klayer: Complete\\n")\n'
        # Exercise the incremental UTF-8 decoder as well as the escape parser.
        'os.write(1,b"\\xe2"); time.sleep(.05); os.write(1,b"\\x9c\\x94 Done\\n")\n'
    )
    result = remote_probe.run_stream([sys.executable, '-c', script], emit, timeout=3)
    assert result == 'layer: Complete\n✔ Done\n'
    assert render(e['text'] for e in events if e['kind'] == 'output') == result
    assert acknowledged.exists()


def test_process_failure_retains_readable_error_without_old_frames():
    script = 'import os; os.write(2,b"Waiting\\r\\x1b[2Kregistry unavailable\\n"); raise SystemExit(1)'
    with pytest.raises(RuntimeError, match='registry unavailable') as error:
        remote_probe.run_stream([sys.executable, '-c', script], lambda e: None, timeout=3)
    assert 'Waiting' not in str(error.value) and '\x1b' not in str(error.value)


@pytest.mark.parametrize('disconnect', [False, True])
def test_live_job_and_reopened_result_keep_rendered_progress(client, monkeypatch, disconnect):
    monkeypatch.setattr(store, 'DEMO', False)
    ready, release = threading.Event(), threading.Event()
    def request(server, payload, on_event=None):
        on_event({'kind': 'step', 'completed': 0, 'total': 2, 'label': 'pull · stack'})
        on_event({'kind': 'output', 'text': 'layer: Downloading\r\n\x1b['})
        on_event({'kind': 'output', 'text': 'A\r\x1b[2Klayer: Complete\r\n'})
        on_event({'kind': 'completed', 'completed': 1, 'total': 2})
        on_event({'kind': 'step', 'completed': 1, 'total': 2, 'label': 'up · stack'})
        on_event({'kind': 'output', 'text': 'web: Starting\r\n\x1b[A\r\x1b[2Kweb: Started\r\n'})
        on_event({'kind': 'completed', 'completed': 2, 'total': 2})
        ready.set()
        assert release.wait(3)
        if disconnect:
            raise RuntimeError('SSH disconnected')
        return {'ok': True, 'output': 'fallback must not replace the live terminal'}
    monkeypatch.setattr(module.ssh, 'request', request)
    user = store.one('SELECT * FROM users WHERE name="admin"')
    job_id = module.queue_job('atlas', 'pull_up', ['group:immich'], user, [])['id']
    try:
        assert ready.wait(2)
        live = client.get('/api/jobs/' + job_id).json()
        assert live['status'] == 'running'
        assert live['output'] == 'pull · stack\nlayer: Complete\n\nup · stack\nweb: Started\n'
    finally:
        release.set()
    finished = wait_job(client, job_id)
    assert finished['status'] == ('failed' if disconnect else 'succeeded')
    assert finished['output'].startswith(live['output'])
    if disconnect:
        assert finished['output'].endswith('SSH disconnected')
    assert client.get('/api/jobs/' + job_id).json()['output'] == finished['output']
