#!/usr/bin/env python
# coding: utf-8

# In[1]:


#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import threading
import shlex
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed

import paramiko  # pip install paramiko

# === Tkinter GUI ===
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

# ------------------------------------------------------------
# 모니터링 대상 서버
# ------------------------------------------------------------
# - total_cores: 알면 정수로 직접 지정해도 되고, None이면 자동 검출합니다.
# - auto_detect: True이면 total_cores가 None일 때 원격 명령으로 자동 검출합니다(1회 캐시).
SERVERS = [
    # 1) LSF
    {
        "name": "cae21",
        "type": "LSF",
        "host": "192.8.100.10",
        "port": 22,
        "username_env": "HPC_LSF1_USER",
        "password_env": "HPC_LSF1_PASS",
        "total_cores": None,      # 예: 1536
        "auto_detect": True,
    },
    # 2) PBS
    {
        "name": "cae25",
        "type": "PBS",
        "host": "192.8.200.134",
        "port": 22,
        "username_env": "HPC_PBS1_USER",
        "password_env": "HPC_PBS1_PASS",
        "total_cores": None,      # 예: 1024
        "auto_detect": True,
    },
    # 3) PBS
    {
        "name": "tca17",
        "type": "PBS",
        "host": "192.8.200.213",
        "port": 22,
        "username_env": "HPC_PBS2_USER",
        "password_env": "HPC_PBS2_PASS",
        "total_cores": None,      # 예: 2048
        "auto_detect": True,
    },
]

DEFAULT_INTERVAL = 30  # 새로고침 주기(초)

# ------------------------------------------------------------
# SSH 유틸 (사용자 RC 비로딩 + 전역 프로필만 로드)
# ------------------------------------------------------------
class SSHRunner:
    def __init__(self, host, port=22, username=None, password=None, pkey_path=None, timeout=20):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.pkey_path = pkey_path
        self.timeout = timeout

    def run(self, command):
        """
        Returns (exit_code, stdout, stderr)
        - ~/.bashrc 등 사용자 RC를 읽지 않도록 --noprofile --norc
        - 전역(/etc/profile)만 로드해서 PATH/LSF/PBS 환경 확보
        """
        client = None
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            if self.pkey_path and os.path.exists(self.pkey_path):
                key = paramiko.RSAKey.from_private_key_file(self.pkey_path)
                client.connect(self.host, port=self.port, username=self.username, pkey=key, timeout=self.timeout)
            else:
                client.connect(self.host, port=self.port, username=self.username, password=self.password, timeout=self.timeout)

            wrapped_cmd = "source /etc/profile >/dev/null 2>&1; " + command
            wrapped = f"/bin/bash --noprofile --norc -c {shlex.quote(wrapped_cmd)}"

            stdin, stdout, stderr = client.exec_command(wrapped, timeout=self.timeout)
            out = stdout.read().decode(errors="replace")
            err = stderr.read().decode(errors="replace")
            code = stdout.channel.recv_exit_status()
            return code, out, err
        except Exception as e:
            return -1, "", f"{type(e).__name__}: {e}"
        finally:
            try:
                if client:
                    client.close()
            except Exception:
                pass

# ------------------------------------------------------------
# 공용 변환/정규화 유틸
# ------------------------------------------------------------
def hms_to_seconds(hms_str):
    """H:MM[:SS], HH:MM:SS, D-HH:MM:SS 등 → 초"""
    if not hms_str:
        return 0
    s = hms_str.strip()
    if s.isdigit():
        return int(s)
    s = s.replace("d-", ":").replace("D-", ":").replace(" ", "")
    parts = [p for p in s.split(":") if p != ""]
    try:
        parts = list(map(int, parts))
    except Exception:
        return 0
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        h, m = parts
        return h*3600 + m*60
    if len(parts) == 3:
        h, m, sec = parts
        return h*3600 + m*60 + sec
    if len(parts) == 4:
        d, h, m, sec = parts
        return d*86400 + h*3600 + m*60 + sec
    return 0

def minutes_from_time_str(time_str):
    """시간 문자열 → 분 단위(정수) 문자열로 변환"""
    seconds = hms_to_seconds(time_str)
    minutes = int(seconds // 60)
    return str(minutes)

def parse_any_mem_to_g(mem_str):
    """
    다양한 메모리 표기(8618044kb, 302 Mbytes, 125 G, 128GB 등)를 모두 G로 환산 (소수 2자리).
    미해석 시 원문 반환.
    """
    if not mem_str:
        return ""
    s = mem_str.strip()

    m = re.match(r"^\s*([\d\.]+)\s*([kmgtp]?b|[kmgt]bytes|[kmgt])\s*$", s, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit in ["k", "kb", "kbytes"]:
            g = val / (1024**2)
        elif unit in ["m", "mb", "mbytes"]:
            g = val / 1024.0
        elif unit in ["g", "gb", "gbytes"]:
            g = val
        elif unit in ["t", "tb", "tbytes"]:
            g = val * 1024.0
        elif unit in ["p", "pb", "pbytes"]:
            g = val * 1024.0 * 1024.0
        else:
            return s
        return f"{g:.2f}G"

    m2 = re.search(r"([\d\.]+)\s*(K|M|G|T|P)(?:bytes)?", s, re.IGNORECASE)
    if m2:
        val = float(m2.group(1))
        unit = m2.group(2).lower()
        if unit == "k":
            g = val / (1024**2)
        elif unit == "m":
            g = val / 1024.0
        elif unit == "g":
            g = val
        elif unit == "t":
            g = val * 1024.0
        elif unit == "p":
            g = val * 1024.0 * 1024.0
        else:
            return s
        return f"{g:.2f}G"

    m3 = re.search(r"([\d\.]+)\s*(k|kb|m|mb|g|gb|t|tb)", s, re.IGNORECASE)
    if m3:
        val = float(m3.group(1))
        unit = m3.group(2).lower()
        if unit in ["k", "kb"]:
            g = val / (1024**2)
        elif unit in ["m", "mb"]:
            g = val / 1024.0
        elif unit in ["g", "gb"]:
            g = val
        elif unit in ["t", "tb"]:
            g = val * 1024.0
        else:
            return s
        return f"{g:.2f}G"

    return s

def normalize_status_for_scheduler(raw_stat, scheduler_type):
    """
    LSF/PBS 상태를 공통 용어로 통일:
      RUN, PEND, HOLD, SUSP, START, EXIT, DONE, (그 외: 원문 또는 OTHER)
    """
    if not raw_stat:
        return ""
    s = (raw_stat or "").strip().upper()
    stype = (scheduler_type or "").strip().upper()

    if stype.startswith("PBS"):
        mapping = {
            "R": "RUN", "Q": "PEND", "W": "PEND", "H": "HOLD",
            "E": "EXIT", "B": "START", "T": "EXIT"
        }
        return mapping.get(s, s)
    else:
        # LSF
        if s in ["SSUSP", "USUSP", "PSUSP"]:
            return "SUSP"
        return s

def to_int_safe(x, default=0):
    try:
        return int(str(x).strip())
    except Exception:
        return default

# ------------------------------------------------------------
# LSF 수집
# ------------------------------------------------------------
def parse_lsf_o_usage(out_text):
    """
    bjobs -hms -X -o "jobid user queue stat job_name exec_host cpu_used run_time max_mem slots delimiter='|'"
    - time: run_time → 분(min)으로 변환 (표시는 나중에)
    - cores_alloc = slots
    - memory(used) = max_mem (표시 시 G로 환산)
    """
    rows = []
    lines = [ln.strip() for ln in out_text.splitlines() if ln.strip()]
    if not lines:
        return rows

    headers = [h.strip().lower() for h in lines[0].split("|")]
    idx = {h: i for i, h in enumerate(headers)}

    def get(parts, k, default=""):
        return parts[idx[k]].strip() if k in idx and idx[k] < len(parts) else default

    for ln in lines[1:]:
        parts = [p.strip() for p in ln.split("|")]
        jobid     = get(parts, "jobid")
        user      = get(parts, "user")
        queue     = get(parts, "queue")
        stat      = get(parts, "stat")
        job_name  = get(parts, "job_name") or get(parts, "jobname")
        run_time  = get(parts, "run_time")
        max_mem   = get(parts, "max_mem")
        slots     = get(parts, "slots")

        rows.append({
            "id": jobid,
            "user": user,
            "queue": queue,
            "job_name": job_name or "",
            "cores_alloc": slots or "",
            "memory_used": max_mem or "",
            "status": stat,
            "time": run_time or "",
        })
    return rows

def fetch_lsf_jobs(runner, all_users=True):
    user_opt = "-u all" if all_users else ""
    fmt = "jobid user queue stat job_name exec_host run_time max_mem slots delimiter='|'"

    # 1) -hms -X + delimiter
    code, out, err = runner.run(f'bjobs -hms {user_opt} -X -o "{fmt}"')
    text = (out or "") + (err or "")
    if code != 0 and "delimiter" in text.lower():
        fmt_nodelim = "jobid user queue stat job_name exec_host run_time max_mem slots"
        code, out, err = runner.run(f'bjobs -hms {user_opt} -X -o "{fmt_nodelim}"')

    # 2) -X 미지원 폴백
    if code != 0 or not (out or "").strip():
        code, out, err = runner.run(f'bjobs -hms {user_opt} -o "{fmt}"')
        text = (out or "") + (err or "")
        if code != 0 and "delimiter" in text.lower():
            fmt_nodelim = "jobid user queue stat job_name exec_host run_time max_mem slots"
            code, out, err = runner.run(f'bjobs -hms {user_opt} -o "{fmt_nodelim}"')

    if code != 0:
        raise RuntimeError(f"bjobs 실패: {err or out}")

    return parse_lsf_o_usage(out)

# ------------------------------------------------------------
# PBS 수집
# ------------------------------------------------------------
def parse_pbs_json(out_json):
    data = json.loads(out_json)
    jobs = data.get("Jobs") or {}
    rows = []

    for job_id, attrs in jobs.items():
        owner = attrs.get("Job_Owner", "")
        user = owner.split("@")[0] if owner else ""
        queue = attrs.get("queue", "")
        state = attrs.get("job_state", "")
        job_name = attrs.get("Job_Name", "") or attrs.get("jobname", "")

        rlist = attrs.get("Resource_List", {}) or {}
        cores_alloc = ""
        if "ncpus" in rlist:
            cores_alloc = str(rlist["ncpus"])
        else:
            sel = str(rlist.get("select", ""))
            if "ncpus=" in sel:
                n = 0
                for frag in sel.split("+"):
                    m = re.search(r"ncpus=(\d+)", frag)
                    if m:
                        n += int(m.group(1))
                if n > 0:
                    cores_alloc = str(n)

        rused = attrs.get("resources_used", {}) or {}
        mem_used = rused.get("mem", "") or ""
        wall = rused.get("walltime", "") or ""

        rows.append({
            "id": job_id,
            "user": user,
            "queue": queue,
            "job_name": job_name,
            "cores_alloc": cores_alloc,
            "memory_used": mem_used,
            "status": state,
            "time": wall,
        })
    return rows

def parse_pbs_text_full(out_text):
    rows = []
    current = {}
    for ln in out_text.splitlines():
        ln = ln.rstrip()
        if not ln:
            continue
        if ln.lower().startswith("job id") or ln.lower().startswith("jobid"):
            if current:
                rows.append(current)
            current = {"id": ln.split(":", 1)[1].strip()}
            continue
        m = re.match(r"^\s*([\w\.\-]+)\s*=\s*(.*)$", ln)
        if m and current is not None:
            k, v = m.group(1), m.group(2).strip()
            current[k] = v
    if current:
        rows.append(current)

    normalized = []
    for j in rows:
        owner = j.get("Job_Owner", "")
        user = owner.split("@")[0] if owner else j.get("User_List", "")
        queue = j.get("queue", "")
        state = j.get("job_state", "")
        job_name = j.get("Job_Name", "") or j.get("jobname", "")

        cores_alloc = ""
        rlist_ncpus = j.get("Resource_List.ncpus")
        if rlist_ncpus:
            cores_alloc = str(rlist_ncpus)
        else:
            sel = j.get("Resource_List.select", "")
            if "ncpus=" in sel:
                n = 0
                for frag in sel.split("+"):
                    m = re.search(r"ncpus=(\d+)", frag)
                    if m:
                        n += int(m.group(1))
                if n > 0:
                    cores_alloc = str(n)

        mem_used = j.get("resources_used.mem", "") or ""
        wall = j.get("resources_used.walltime", "") or ""

        normalized.append({
            "id": j.get("id") or j.get("Job_Id", ""),
            "user": user,
            "queue": queue,
            "job_name": job_name,
            "cores_alloc": cores_alloc,
            "memory_used": mem_used,
            "status": state,
            "time": wall,
        })
    return normalized

def fetch_pbs_jobs(runner):
    code, out, err = runner.run("qstat -f -F json")
    if code == 0 and out.strip():
        try:
            return parse_pbs_json(out)
        except Exception:
            pass

    code, out, err = runner.run("qstat -f")
    if code == 0 and out.strip():
        try:
            return parse_pbs_text_full(out)
        except Exception:
            pass

    code, out, err = runner.run("qstat -a")
    if code != 0:
        raise RuntimeError(f"qstat 실패: {err or out}")
    rows = []
    for ln in out.splitlines():
        if not ln.strip() or ln.lower().startswith("job id") or re.match(r"^-+$", ln.strip()):
            continue
        parts = re.split(r"\s+", ln.strip())
        if len(parts) < 6:
            continue
        job_id = parts[0]
        user   = parts[1] if len(parts) > 1 else ""
        state  = ""
        queue  = ""
        for idx in range(len(parts) - 1, -1, -1):
            if re.fullmatch(r"[RQHCEBWTX]", parts[idx]):
                state = parts[idx]
                if idx + 1 < len(parts):
                    queue = parts[idx + 1]
                elif idx - 1 >= 0:
                    queue = parts[idx - 1]
                break
        rows.append({
            "id": job_id,
            "user": user,
            "queue": queue,
            "job_name": "",
            "cores_alloc": "",
            "memory_used": "",
            "status": state,
            "time": "",
        })
    return rows

# ------------------------------------------------------------
# (NEW) 총 코어 수 자동 검출
# ------------------------------------------------------------
def detect_total_cores_lsf(runner):
    """
    LSF: `lshosts -w` 파싱하여 ncpus 합산
    """
    code, out, err = runner.run("lshosts -w")
    if code != 0 or not out.strip():
        # 폴백: 폭 제한 출력
        code, out, err = runner.run("lshosts")
        if code != 0 or not out.strip():
            return None

    lines = [ln for ln in out.splitlines() if ln.strip()]
    if not lines:
        return None

    header = re.split(r"\s+", lines[0].strip())
    try:
        ncpus_idx = [h.lower() for h in header].index("ncpus")
    except ValueError:
        return None

    total = 0
    for ln in lines[1:]:
        parts = re.split(r"\s+", ln.strip())
        if len(parts) <= ncpus_idx:
            continue
        val = parts[ncpus_idx]
        try:
            total += int(val)
        except Exception:
            continue
    return total if total > 0 else None

def detect_total_cores_pbs(runner):
    """
    PBS: `pbsnodes -a -F json` → JSON 파싱, 실패 시 `pbsnodes -a` 텍스트 파싱
    - state에 'down' 또는 'offline' 포함 노드는 제외
    - resources_available.ncpus 합산
    """
    code, out, err = runner.run("pbsnodes -a -F json")
    if code == 0 and out.strip():
        try:
            data = json.loads(out)
            nodes_obj = data.get("nodes")
            total = 0
            if isinstance(nodes_obj, dict):
                it = nodes_obj.values()
            elif isinstance(nodes_obj, list):
                it = nodes_obj
            else:
                it = []
            for nd in it:
                state = (nd.get("state") or "").lower()
                if "down" in state or "offline" in state:
                    continue
                res = nd.get("resources_available") or {}
                ncpus = res.get("ncpus")
                if ncpus is None:
                    ncpus = nd.get("resources_available.ncpus")
                try:
                    total += int(ncpus)
                except Exception:
                    pass
            if total > 0:
                return total
        except Exception:
            pass

    code, out, err = runner.run("pbsnodes -a")
    if code != 0 or not out.strip():
        return None

    total = 0
    block = []
    lines = out.splitlines()

    def flush_block(bl):
        nonlocal total
        if not bl:
            return
        text = "\n".join(bl)
        m_state = re.search(r"^\s*state\s*=\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
        state = (m_state.group(1).strip().lower() if m_state else "")
        if "down" in state or "offline" in state:
            return
        for m in re.finditer(r"resources_available\.ncpus\s*=\s*(\d+)", text):
            try:
                total += int(m.group(1))
            except Exception:
                pass

    for ln in lines + [""]:
        if ln.strip() == "":
            flush_block(block)
            block = []
        else:
            block.append(ln)

    return total if total > 0 else None

def detect_total_cores_for_server(cfg, username, password):
    try:
        runner = SSHRunner(cfg["host"], cfg.get("port", 22), username, password, cfg.get("pkey_path"))
        if cfg["type"].upper() == "LSF":
            return detect_total_cores_lsf(runner)
        else:
            return detect_total_cores_pbs(runner)
    except Exception:
        return None

# ------------------------------------------------------------
# 수집자
# ------------------------------------------------------------
def gather_all(credentials_map, all_users=True):
    rows = []
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=min(8, len(SERVERS))) as ex:
        futs = []
        for cfg in SERVERS:
            def task(cfg=cfg):
                name = cfg["name"]
                scheduler_type = cfg["type"]
                username = credentials_map.get(name, (None, None))[0] or os.environ.get(cfg["username_env"])
                password = credentials_map.get(name, (None, None))[1] or os.environ.get(cfg["password_env"])
                if not username or not password:
                    with lock:
                        rows.append({
                            "id": "-", "user": "-", "queue": "-", "job_name": f"{name}",
                            "cores_alloc": "", "memory_used": "",
                            "status": "NO-LOGIN", "time": "0",
                            "server_name": name,
                            "scheduler_type": scheduler_type,
                        })
                    return
                runner = SSHRunner(cfg["host"], cfg.get("port", 22), username, password, cfg.get("pkey_path"))
                try:
                    if scheduler_type.upper() == "LSF":
                        items = fetch_lsf_jobs(runner, all_users)
                    else:
                        items = fetch_pbs_jobs(runner)
                    for j in items:
                        j["server_name"] = name
                        j["scheduler_type"] = scheduler_type
                    with lock:
                        rows.extend(items)
                except Exception:
                    with lock:
                        rows.append({
                            "id": "-", "user": "-", "queue": "-", "job_name": f"{name} ({cfg['host']})",
                            "cores_alloc": "", "memory_used": "",
                            "status": "ERR", "time": "0",
                            "server_name": name,
                            "scheduler_type": scheduler_type,
                        })
            futs.append(ex.submit(task))
        for _ in as_completed(futs):
            pass
    return rows

# ------------------------------------------------------------
# (NEW) 바차트 컴포넌트
# ------------------------------------------------------------
class MiniBar(ttk.Frame):
    def __init__(self, master, width=560, height=16):
        super().__init__(master)
        self.width = width
        self.height = height
        self.canvas = tk.Canvas(self, width=width, height=height, highlightthickness=0, bg="white")
        self.canvas.pack(side=tk.LEFT)
        self.label = ttk.Label(self, text="--/-- (--%)")
        self.label.pack(side=tk.LEFT, padx=6)

        self.base_color = "#e9ecef"
        self.fill_color = "#3b82f6"
        self.border_color = "#cbd5e1"
        self.approx_color = "#94a3b8"

    def update_value(self, used, total):
        self.canvas.delete("all")
        w = self.width
        h = self.height
        pad = 1
        self.canvas.create_rectangle(pad, pad, w - pad, h - pad, fill=self.base_color, outline=self.border_color)
        if isinstance(total, int) and total > 0:
            ratio = min(max(used / total, 0.0), 1.0)
            fill_w = pad + int((w - 2*pad) * ratio)
            self.canvas.create_rectangle(pad, pad, fill_w, h - pad, fill=self.fill_color, outline="")
            pct = (used / total) * 100.0
            self.label.config(text=f"{used}/{total} ({pct:.1f}%)")
        else:
            approx = min(max(used, 0), 100)
            ratio = approx / 100.0
            fill_w = pad + int((w - 2*pad) * ratio)
            self.canvas.create_rectangle(pad, pad, fill_w, h - pad, fill=self.approx_color, outline="")
            self.label.config(text=f"{used}/N/A (--%)")

# ------------------------------------------------------------
# GUI
# ------------------------------------------------------------
class JobMonitorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HPC Job Monitor (LSF + PBS)")
        self.geometry("1180x780")
        self.minsize(980, 560)  # 너무 작은 창에서 레이아웃 붕괴 방지
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.interval_var = tk.IntVar(value=DEFAULT_INTERVAL)
        self.mine_only_var = tk.BooleanVar(value=False)

        self.credentials = {}
        self.server_order = {cfg["name"]: i for i, cfg in enumerate(SERVERS)}
        self.total_core_cache = {}

        # 최상위 컨테이너(grid 사용)
        root_container = ttk.Frame(self)
        root_container.pack(fill=tk.BOTH, expand=True)
        root_container.grid_columnconfigure(0, weight=1)
        # row2(테이블)만 가변, row3(상태바)는 고정(minsize)
        root_container.grid_rowconfigure(2, weight=1)
        root_container.grid_rowconfigure(3, weight=0, minsize=28)  # ★ 하단 상태바 행 고정 높이

        # === [row=0] 상단 바차트 ===
        self.util_bars = {}
        util_frame = ttk.LabelFrame(root_container, text="서버별 코어 점유율")
        util_frame.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        for cfg in SERVERS:
            name = cfg["name"]
            total_cores = cfg.get("total_cores")
            row = ttk.Frame(util_frame)
            row.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)
            ttk.Label(row, text=f"{name}", width=10).pack(side=tk.LEFT)
            bar = MiniBar(row, width=560, height=16)
            bar.pack(side=tk.LEFT, padx=8)
            bar.update_value(0, total_cores if isinstance(total_cores, int) else None)
            self.util_bars[name] = {"bar": bar, "total": total_cores}

        # === [row=1] 컨트롤 ===
        control_frame = ttk.Frame(root_container)
        control_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=(4, 6))
        ttk.Label(control_frame, text="새로고침(초):").pack(side=tk.LEFT)
        ttk.Entry(control_frame, width=6, textvariable=self.interval_var).pack(side=tk.LEFT, padx=(4,12))
        ttk.Checkbutton(control_frame, text="LSF 내 작업만(--mine-only)", variable=self.mine_only_var).pack(side=tk.LEFT, padx=8)
        ttk.Button(control_frame, text="자격증명 입력", command=self.ask_credentials).pack(side=tk.LEFT, padx=8)
        ttk.Button(control_frame, text="지금 새로고침", command=self.manual_refresh).pack(side=tk.LEFT, padx=8)

        # === [row=2] 트리뷰(가변) ===
        center_frame = ttk.Frame(root_container)
        center_frame.grid(row=2, column=0, sticky="nsew", padx=8, pady=8)
        center_frame.rowconfigure(0, weight=1)
        center_frame.columnconfigure(0, weight=1)

        cols = ("id", "user", "queue", "job_name", "cores_alloc", "memory_used", "status", "time_min")
        self.tree = ttk.Treeview(center_frame, columns=cols, show="headings", height=24)
        self.tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(center_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(center_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        headings = {
            "id": "ID", "user": "사용자", "queue": "QUEUE", "job_name": "JOB 이름",
            "cores_alloc": "코어", "memory_used": "메모리(GB)", "status": "상태", "time_min": "경과시간(분)",
        }
        widths = {"id":110,"user":120,"queue":120,"job_name":320,"cores_alloc":110,"memory_used":130,"status":100,"time_min":110}
        center_cols = {"id","user","queue","status"}
        for c in cols:
            self.tree.heading(c, text=headings[c])
            anchor = tk.W if c=="job_name" else (tk.CENTER if c in center_cols else tk.E)
            self.tree.column(c, width=widths[c], anchor=anchor, stretch=(c=="job_name"))
        self.tree.tag_configure("sep", background="#f2f2f2", foreground="#999999")

        # === [row=3] 하단 상태바(고정) ===
        bottom_status = ttk.Frame(root_container, height=28)
        bottom_status.grid(row=3, column=0, sticky="ew", padx=8, pady=(0,6))
        bottom_status.grid_propagate(False)  # ★ 프레임 자체 높이를 유지
        self.status_var = tk.StringVar(value="준비")
        self.status_label = ttk.Label(bottom_status, textvariable=self.status_var, anchor="w")
        self.status_label.pack(side=tk.LEFT)

        # 주기적 새로고침
        self._stop = False
        self._refreshing = False
        self.after(500, self.refresh_loop)

    def on_close(self):
        self._stop = True
        self.destroy()

    def ask_credentials(self):
        for cfg in SERVERS:
            name = cfg["name"]
            if self.credentials.get(name):
                continue
            user = os.environ.get(cfg["username_env"])
            pwd  = os.environ.get(cfg["password_env"])
            if not user:
                user = simpledialog.askstring("자격증명", f"[{name}] SSH 사용자명:", parent=self)
                if user is None:
                    continue
            if not pwd:
                pwd = simpledialog.askstring("자격증명", f"[{name}] {user}@{cfg['host']} 암호:", parent=self, show="*")
                if pwd is None:
                    continue
            self.credentials[name] = (user, pwd)

    def manual_refresh(self):
        if not self._refreshing:
            self._refresh_once()

    def refresh_loop(self):
        if not self._stop and not self._refreshing:
            self._refresh_once()
        self.after(max(1000, self.interval_var.get() * 1000), self.refresh_loop)

    # ----- 상태/정렬 우선순위 -----
    def status_weight(self, unified_status):
        order = {"RUN":0,"START":1,"PEND":2,"HOLD":3,"SUSP":4,"DONE":5,"EXIT":6,"NO-LOGIN":8,"ERR":9}
        return order.get((unified_status or "").upper(), 7)

    def _id_sort_key(self, id_str: str):
        s = id_str or ""
        m = re.search(r"\d+", s)
        if m:
            return (0, int(m.group()))
        return (1, s)

    def sort_key(self, x):
        server_name = x.get("server_name", "")
        server_w = self.server_order.get(server_name, 999)
        status_w = self.status_weight(x.get("status", ""))
        id_w = self._id_sort_key(x.get("id", ""))
        return (server_w, status_w, id_w, x.get("user",""), x.get("queue",""))

    # ----- 바차트 업데이트 -----
    def update_utilization_bars(self, used_by_server, override_totals=None):
        for cfg in SERVERS:
            name = cfg["name"]
            rec = self.util_bars.get(name)
            if not rec:
                continue
            total = None
            if override_totals and name in override_totals and isinstance(override_totals[name], int):
                total = override_totals[name]
            elif name in self.total_core_cache:
                total = self.total_core_cache[name]
            else:
                t_cfg = cfg.get("total_cores")
                total = t_cfg if isinstance(t_cfg, int) else None
            used = to_int_safe(used_by_server.get(name, 0), 0)
            rec["bar"].update_value(used, total)
            rec["total"] = total
            if isinstance(total, int):
                self.total_core_cache[name] = total
                cfg["total_cores"] = total

    # ----- 총 코어 수 자동 검출 (1회) -----
    def detect_totals_if_needed(self):
        detected = {}
        for cfg in SERVERS:
            name = cfg["name"]
            if isinstance(cfg.get("total_cores"), int):
                self.total_core_cache[name] = cfg["total_cores"]
                continue
            if name in self.total_core_cache:
                continue
            if not cfg.get("auto_detect", True):
                continue
            username = self.credentials.get(name, (None, None))[0] or os.environ.get(cfg["username_env"])
            password = self.credentials.get(name, (None, None))[1] or os.environ.get(cfg["password_env"])
            if not username or not password:
                continue
            total = detect_total_cores_for_server(cfg, username, password)
            if isinstance(total, int) and total > 0:
                self.total_core_cache[name] = total
                detected[name] = total
        return detected

    def _refresh_once(self):
        self._refreshing = True
        self.status_var.set("수집 중…")
        q = queue.Queue()

        def worker():
            try:
                detected_totals = self.detect_totals_if_needed()
                rows = gather_all(self.credentials, all_users=not self.mine_only_var.get())
                normalized = []
                for r in rows:
                    server_name = r.get("server_name", "")
                    scheduler_type = (r.get("scheduler_type") or "").upper()
                    unified_status = normalize_status_for_scheduler(r.get("status", ""), scheduler_type)
                    time_min = minutes_from_time_str(r.get("time", ""))
                    mem_g = parse_any_mem_to_g(r.get("memory_used", ""))
                    cores_alloc_int = to_int_safe(r.get("cores_alloc", 0), default=0)
                    normalized.append({
                        "server_name": server_name,
                        "scheduler_type": scheduler_type,
                        "id": r.get("id",""),
                        "user": r.get("user",""),
                        "queue": r.get("queue",""),
                        "job_name": r.get("job_name",""),
                        "cores_alloc": str(cores_alloc_int),
                        "cores_alloc_int": cores_alloc_int,
                        "memory_used": mem_g,
                        "status": unified_status,
                        "time_min": time_min,
                    })
                used_by_server = {}
                for r in normalized:
                    if (r.get("status") or "").upper() in ("RUN", "START"):
                        srv = r.get("server_name","")
                        used_by_server[srv] = used_by_server.get(srv, 0) + r.get("cores_alloc_int", 0)
                q.put(("OK", {"rows": normalized, "used": used_by_server, "totals": detected_totals}))
            except Exception as e:
                q.put(("ERR", str(e)))

        threading.Thread(target=worker, daemon=True).start()

        def insert_separator():
            line = "─" * 80
            values = ("", "", "", line, "", "", "", "")
            self.tree.insert("", tk.END, values=values, tags=("sep",))

        def build_server_counts_string(data_rows):
            counts = {cfg["name"]: 0 for cfg in SERVERS}
            for r in data_rows:
                srv = r.get("server_name", "")
                if srv in counts:
                    counts[srv] += 1
            return ", ".join([f"{name}: {counts[name]}" for name in counts])

        def apply_result():
            if q.empty():
                self.after(100, apply_result)
                return
            status, payload = q.get()
            if status == "OK":
                data_rows = payload["rows"]
                used_map = payload["used"]
                totals_map = payload.get("totals", {})

                data_rows.sort(key=self.sort_key)

                self.tree.delete(*self.tree.get_children())
                current_server = None
                for r in data_rows:
                    srv = r["server_name"]
                    if current_server is None:
                        current_server = srv
                    elif srv != current_server:
                        insert_separator()
                        current_server = srv
                    self.tree.insert("", tk.END, values=(
                        r["id"], r["user"], r["queue"], r["job_name"],
                        r["cores_alloc"], r["memory_used"], r["status"], r["time_min"]
                    ))

                self.update_utilization_bars(used_map, override_totals=totals_map)

                server_counts_text = build_server_counts_string(data_rows)
                self.status_var.set(
                    f"갱신 완료: {time.strftime('%Y-%m-%d %H:%M:%S')}  "
                    f"(총 {len(data_rows)}개)  |  {server_counts_text}"
                )
                
                # 월별 로그 기록
                log_utilization(used_map, self.total_core_cache)
            else:
                self.status_var.set(f"오류: {payload}")
                messagebox.showerror("오류", payload)
            self._refreshing = False

        self.after(100, apply_result)

def log_utilization(used_by_server, totals):
    """
    각 서버별 점유율을 서버별 월별 로그 파일에 기록
    """
    logs_dir = "server_logs"
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir)
    
    # 타임스탬프
    now = time.localtime()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", now)
    
    # 각 서버별로 로그 기록
    for server, used in used_by_server.items():
        # 서버별 월별 파일명 (예: cae21-2026-05.log)
        log_file = os.path.join(logs_dir, f"{server}-{now.tm_year}-{now.tm_mon:02d}.log")
        total = totals.get(server)
        if total and total > 0:
            pct = (used / total) * 100.0
            log_line = f"[{timestamp}] {server}: {used}/{total} ({pct:.1f}%)\n"
        else:
            log_line = f"[{timestamp}] {server}: {used}/N/A\n"
        
        # 파일에 추가
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(log_line)

# ------------------------------------------------------------

# ------------------------------------------------------------
if __name__ == "__main__":
    try:
        app = JobMonitorApp()
        app.mainloop()
    except Exception as e:
        try:
            messagebox.showerror("치명적 오류", str(e))
        except Exception:
            print("치명적 오류:", e)


# In[ ]:




