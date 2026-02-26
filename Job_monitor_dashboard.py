#!/usr/bin/env python
# coding: utf-8

# In[5]:


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
SERVERS = [
    # 1) LSF
    {
        "name": "cae21",
        "type": "LSF",
        "host": "192.8.100.10",
        "port": 22,
        "username_env": "HPC_LSF1_USER",
        "password_env": "HPC_LSF1_PASS",
    },
    # 2) PBS
    {
        "name": "cae25",
        "type": "PBS",
        "host": "192.8.200.134",
        "port": 22,
        "username_env": "HPC_PBS1_USER",
        "password_env": "HPC_PBS1_PASS",
    },
    # 3) PBS
    {
        "name": "tca17",
        "type": "PBS",
        "host": "192.8.200.213",
        "port": 22,
        "username_env": "HPC_PBS2_USER",
        "password_env": "HPC_PBS2_PASS",
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
    s = raw_stat.strip().upper()

    if scheduler_type.upper().startswith("PBS"):
        mapping = {"R": "RUN", "Q": "PEND", "W": "PEND", "H": "HOLD", "E": "EXIT", "B": "START", "T": "EXIT"}
        return mapping.get(s, s)
    else:
        if s in ["SSUSP", "USUSP", "PSUSP"]:
            return "SUSP"
        return s

# ------------------------------------------------------------
# LSF 수집
# ------------------------------------------------------------
def parse_lsf_o_usage(out_text):
    """
    bjobs -hms -X -o "jobid user queue stat job_name exec_host cpu_used run_time max_mem slots delimiter='|'"
    - time: run_time → 분(min)으로 변환
    - cores_alloc = slots
    - memory(used) = max_mem (표시 시 G로 환산)
    """
    rows = []
    lines = [ln.strip() for ln in out_text.splitlines() if ln.strip()]
    if not lines:
        return rows

    headers = [h.strip().lower() for h in lines[0].split("|")]
    idx = {h:i for i, h in enumerate(headers)}

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
    # **중요**: -hms로 시간 필드를 hh:mm:ss로 강제(IBM 문서) → 분 변환이 정확해짐 [1](https://www.ibm.com/docs/en/spectrum-lsf/10.1.0?topic=bjobs-options)
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
                username = credentials_map.get(name, (None, None))[0] or os.environ.get(cfg["username_env"])
                password = credentials_map.get(name, (None, None))[1] or os.environ.get(cfg["password_env"])
                if not username or not password:
                    with lock:
                        rows.append({
                            "id": "-", "user": "-", "queue": "-", "job_name": f"{name}",
                            "cores_alloc": "", "memory_used": "",
                            "status": "NO-LOGIN", "time": "0",
                            "scheduler": name,
                        })
                    return

                runner = SSHRunner(cfg["host"], cfg.get("port", 22), username, password, cfg.get("pkey_path"))
                try:
                    if cfg["type"].upper() == "LSF":
                        items = fetch_lsf_jobs(runner, all_users)
                    else:
                        items = fetch_pbs_jobs(runner)
                    for j in items:
                        j["scheduler"] = name  # 정렬용
                    with lock:
                        rows.extend(items)
                except Exception as e:
                    with lock:
                        rows.append({
                            "id": "-", "user": "-", "queue": "-", "job_name": f"{name} ({cfg['host']})",
                            "cores_alloc": "", "memory_used": "",
                            "status": "ERR", "time": "0",
                            "scheduler": name,
                        })
            futs.append(ex.submit(task))
        for _ in as_completed(futs):
            pass
    return rows

# ------------------------------------------------------------
# GUI
# ------------------------------------------------------------
class JobMonitorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HPC Job Monitor (LSF + PBS)")
        self.geometry("1180x640")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.interval_var = tk.IntVar(value=DEFAULT_INTERVAL)
        self.mine_only_var = tk.BooleanVar(value=False)

        # 자격 증명 보관(필요 시 GUI로 질문)
        self.credentials = {}  # { "LSF-1": (user, pass), ... }

        # 상단 컨트롤
        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)
        ttk.Label(top, text="새로고침(초):").pack(side=tk.LEFT)
        ttk.Entry(top, width=6, textvariable=self.interval_var).pack(side=tk.LEFT, padx=(4,12))
        ttk.Checkbutton(top, text="LSF 내 작업만(--mine-only)", variable=self.mine_only_var).pack(side=tk.LEFT, padx=8)
        ttk.Button(top, text="자격증명 입력", command=self.ask_credentials).pack(side=tk.LEFT, padx=8)
        ttk.Button(top, text="지금 새로고침", command=self.manual_refresh).pack(side=tk.LEFT, padx=8)

        # 표 (cores(used) 제거됨)
        cols = ("id", "user", "queue", "job_name", "cores_alloc", "memory_used", "status", "time_min")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=24)
        self.tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=8)

        # 헤더/폭/정렬:
        #  - job_name: 좌측 정렬
        #  - ID, user, queue, status: 중앙 정렬
        #  - 나머지(cores_alloc, memory_used, time_min): 오른 정렬
        headings = {
            "id": "ID",
            "user": "사용자",
            "queue": "QUEUE",
            "job_name": "JOB 이름",
            "cores_alloc": "코어",
            "memory_used": "메모리(GB)",
            "status": "상태",
            "time_min": "경과시간(분)",
        }
        widths = {
            "id": 110, "user": 120, "queue": 120, "job_name": 320,
            "cores_alloc": 110, "memory_used": 130, "status": 100, "time_min": 110
        }
        center_cols = {"id", "user", "queue", "status"}
        for c in cols:
            self.tree.heading(c, text=headings[c])
            if c == "job_name":
                anchor = tk.W
            elif c in center_cols:
                anchor = tk.CENTER
            else:
                anchor = tk.E
            self.tree.column(c, width=widths[c], anchor=anchor)

        # 상태바
        self.status_var = tk.StringVar(value="준비")
        status_frame = ttk.Frame(self)
        status_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0,6))
        ttk.Label(status_frame, textvariable=self.status_var).pack(side=tk.LEFT)

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

    # ----- 정렬 우선순위 -----
    def scheduler_weight(self, scheduler_name):
        order = {"PBS-1": 0, "PBS-2": 1, "LSF-1": 2}  # 스케줄러 묶음 순서
        return order.get(scheduler_name, 99)

    def status_weight(self, unified_status):
        order = {
            "RUN": 0, "PEND": 1, "HOLD": 2, "SUSP": 3, "START": 4,
            "EXIT": 5, "DONE": 6, "NO-LOGIN": 8, "ERR": 9
        }
        return order.get(unified_status, 7)  # 기타는 중간

    def _refresh_once(self):
        self._refreshing = True
        self.status_var.set("수집 중…")
        q = queue.Queue()

        def worker():
            try:
                rows = gather_all(self.credentials, all_users=not self.mine_only_var.get())

                # --- 정규화 단계 ---
                normalized = []
                for r in rows:
                    sched = r.get("scheduler", "")

                    # status 통일
                    unified_status = normalize_status_for_scheduler(r.get("status", ""), sched if sched else "")

                    # 시간 → 분 단위
                    time_min = minutes_from_time_str(r.get("time", ""))

                    # 메모리 단위 → G
                    mem_g = parse_any_mem_to_g(r.get("memory_used", ""))

                    normalized.append({
                        "scheduler": sched,
                        "id": r.get("id",""),
                        "user": r.get("user",""),
                        "queue": r.get("queue",""),
                        "job_name": r.get("job_name",""),
                        "cores_alloc": r.get("cores_alloc",""),
                        "memory_used": mem_g,
                        "status": unified_status,
                        "time_min": time_min,
                    })

                # --- 정렬 ---
                # 전체적으로 PBS-1 → PBS-2 → LSF-1
                # PBS 그룹 내부: RUN 우선
                # LSF 그룹 내부: ID 오름차순(숫자)
                def sort_key(x):
                    sched = x.get("scheduler","")
                    if sched == "LSF-1":
                        # ID를 숫자로 변환 (숫자 아닌 경우 문자열 비교 보조)
                        try:
                            id_num = int(re.sub(r"[^0-9]", "", x.get("id","")) or 0)
                        except Exception:
                            id_num = 0
                        return (self.scheduler_weight(sched), 0, id_num)
                    else:
                        return (
                            self.scheduler_weight(sched),
                            0 if sched in ("PBS-1","PBS-2") else 1,
                            self.status_weight(x.get("status","")),
                            x.get("queue",""),
                            x.get("user",""),
                            x.get("id",""),
                        )

                normalized.sort(key=sort_key)

                q.put(("OK", normalized))
            except Exception as e:
                q.put(("ERR", str(e)))

        threading.Thread(target=worker, daemon=True).start()

        def apply_result():
            if q.empty():
                self.after(100, apply_result)
                return
            status, payload = q.get()
            if status == "OK":
                self.tree.delete(*self.tree.get_children())
                for r in payload:
                    self.tree.insert("", tk.END, values=(
                        r["id"], r["user"], r["queue"], r["job_name"],
                        r["cores_alloc"], r["memory_used"], r["status"], r["time_min"]
                    ))
                self.status_var.set(f"갱신 완료: {time.strftime('%Y-%m-%d %H:%M:%S')}  (총 {len(payload)}개)")
            else:
                self.status_var.set(f"오류: {payload}")
                messagebox.showerror("오류", payload)
            self._refreshing = False

        self.after(100, apply_result)

# ------------------------------------------------------------
# main
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




