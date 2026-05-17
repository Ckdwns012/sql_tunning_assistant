"""
SQL Tuning Advisor - Oracle + Gemini API
1. SQL 분석 (V$SQL 부하쿼리 모니터링)
2. SQL 입력 → 테이블 파싱
3. 인덱스 조회
4. V$SQL 추적 (채번 + 실행계획)
5. 프롬프트 생성
6. Gemini API 분석

필요 패키지:
  pip install jaydebeapi jpype1 google-genai sqlparse
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import threading
import uuid
import re
import os

try:
    import jaydebeapi
    HAS_JDBC = True
except ImportError:
    HAS_JDBC = False

try:
    from google import genai
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False

try:
    import sqlparse
    HAS_SQLPARSE = True
except ImportError:
    HAS_SQLPARSE = False

DEFAULT_JAR = "/Users/ichangjun/ojdbc8.jar"
ORACLE_DRIVER_CLASS = "oracle.jdbc.OracleDriver"


# ─────────────────────────────────────────────────────────────────────────────
# 1. DB 연결
# ─────────────────────────────────────────────────────────────────────────────

class OracleConnection:
    def __init__(self, host, port, dbname, user, password, jar_path):
        self.host, self.port, self.dbname = host, port, dbname
        self.user, self.password, self.jar_path = user, password, jar_path
        self._conn = None

    def connect(self):
        if not HAS_JDBC:
            raise RuntimeError("jaydebeapi 미설치: pip install jaydebeapi jpype1")
        if not os.path.exists(self.jar_path):
            raise RuntimeError(f"JDBC jar 없음:\n{self.jar_path}")
        jdbc_url = f"jdbc:oracle:thin:@{self.host}:{self.port}:{self.dbname}"
        self._conn = jaydebeapi.connect(
            ORACLE_DRIVER_CLASS, jdbc_url,
            [self.user, self.password], self.jar_path)
        self._conn.jconn.setAutoCommit(False)
        return self

    def execute(self, sql, params=None):
        cur = self._conn.cursor()
        cur.execute(sql, params or [])
        cols = [d[0] for d in cur.description] if cur.description else []
        return cols, cur.fetchall()

    def execute_no_fetch(self, sql):
        cur = self._conn.cursor()
        try:
            cur.execute(sql)
        finally:
            self._conn.rollback()

    def close(self):
        if self._conn:
            self._conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 2. SQL 유틸
# ─────────────────────────────────────────────────────────────────────────────

_SQL_KEYWORDS = {
    "SELECT","WHERE","ON","SET","AND","OR","NOT","IN","IS","NULL",
    "CASE","WHEN","THEN","ELSE","END","AS","BY","ORDER","GROUP",
    "HAVING","UNION","ALL","DISTINCT","EXISTS","BETWEEN","LIKE",
    "INNER","LEFT","RIGHT","FULL","OUTER","CROSS","NATURAL",
    "WITH","ROWNUM","SYSDATE","DUAL","TABLE","VIEW","INDEX",
    "CURRENT_DATE","FETCH","FIRST","ROWS","ONLY","OVER","PARTITION",
    "ROW_NUMBER","RANK","DENSE_RANK","COUNT","SUM","MAX","MIN","AVG",
    "NVL","NVL2","COALESCE","DECODE","TRUNC","TO_CHAR","TO_DATE",
    "SUBSTR","TRIM","LPAD","RPAD","CONCAT","LENGTH","ROUND",
    "ADD_MONTHS","MONTHS_BETWEEN","EXTRACT","CAST",
}

def extract_tables(sql):
    sql_clean = re.sub(r'--[^\n]*', ' ', sql)
    sql_clean = re.sub(r'/\*.*?\*/', ' ', sql_clean, flags=re.DOTALL)
    tables = []
    for m in re.finditer(
        r'(?:FROM|JOIN|UPDATE|INTO)\s+((?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*)',
        sql_clean, re.IGNORECASE):
        tbl = m.group(1).upper().split(".")[-1]
        if tbl not in _SQL_KEYWORDS and len(tbl) > 2:
            tables.append(tbl)
    for m in re.finditer(
        r'(?:FROM|,)\s+((?:[A-Za-z_]\w*\.)?TB_\w*)', sql_clean, re.IGNORECASE):
        tbl = m.group(1).upper().split(".")[-1]
        if tbl not in _SQL_KEYWORDS:
            tables.append(tbl)
    return list(dict.fromkeys(t for t in tables if t.startswith("TB_")))

def build_index_query(tables):
    p = ", ".join(f"'{t}'" for t in tables)
    return f"""SELECT i.TABLE_NAME, i.INDEX_NAME, i.UNIQUENESS, i.STATUS,
    ic.COLUMN_NAME, ic.COLUMN_POSITION, ic.DESCEND
FROM DBA_INDEXES i
JOIN DBA_IND_COLUMNS ic ON i.INDEX_NAME = ic.INDEX_NAME AND i.TABLE_NAME = ic.TABLE_NAME
WHERE i.TABLE_NAME IN ({p})
ORDER BY i.TABLE_NAME, i.INDEX_NAME, ic.COLUMN_POSITION"""

def build_table_stats_query(tables):
    p = ", ".join(f"'{t}'" for t in tables)
    return f"""SELECT TABLE_NAME, NUM_ROWS, BLOCKS, AVG_ROW_LEN, LAST_ANALYZED, PARTITIONED
FROM DBA_TABLES WHERE TABLE_NAME IN ({p}) ORDER BY TABLE_NAME"""

def build_index_stats_query(tables):
    p = ", ".join(f"'{t}'" for t in tables)
    return f"""SELECT TABLE_NAME, INDEX_NAME, BLEVEL, DISTINCT_KEYS, CLUSTERING_FACTOR,
    NUM_ROWS, LAST_ANALYZED, VISIBILITY
FROM DBA_INDEXES WHERE TABLE_NAME IN ({p}) ORDER BY TABLE_NAME, INDEX_NAME"""

def inject_comment(sql, tag):
    return f"/* TUNING_TAG:{tag} */\n{sql.strip()}"

def build_vsql_query(tag):
    return f"""SELECT SQL_ID, CHILD_NUMBER, EXECUTIONS, ELAPSED_TIME, BUFFER_GETS
FROM V$SQL WHERE SQL_TEXT LIKE '%TUNING_TAG:{tag}%'
  AND SQL_TEXT NOT LIKE '%V$SQL%' AND EXECUTIONS > 0
ORDER BY ELAPSED_TIME DESC"""

def build_explain_query(sql_id, child_number):
    return f"SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR('{sql_id}', {child_number}, 'ALL'))"


# ─────────────────────────────────────────────────────────────────────────────
# V$SQL 부하쿼리 (TB_ 업무 테이블 사용 SQL만)
# ─────────────────────────────────────────────────────────────────────────────

_BIZ_WHERE = """
WHERE EXECUTIONS > 0
  AND UPPER(SQL_TEXT) LIKE '%TB_%'
  AND SQL_TEXT NOT LIKE '%V$%'
  AND SQL_TEXT NOT LIKE '%TUNING_TAG%'
  AND SQL_TEXT NOT LIKE '%DBA_%'
  AND SQL_TEXT NOT LIKE '%ALL_TAB%'
  AND SQL_TEXT NOT LIKE '%ALL_IND%'
  AND SQL_TEXT NOT LIKE '%USER_TAB%'
  AND SQL_TEXT NOT LIKE '%USER_IND%'
  AND SQL_TEXT NOT LIKE '%SYS.%'
  AND SQL_TEXT NOT LIKE '%SYSTEM.%'
  AND SQL_TEXT NOT LIKE '%DBMS_%'
  AND SQL_TEXT NOT LIKE '%X$%'
  AND SQL_TEXT NOT LIKE '%GV$%'
  AND SQL_TEXT NOT LIKE '%WRI$%'
  AND SQL_TEXT NOT LIKE '%WRH$%'
  AND SQL_TEXT NOT LIKE '%WRM$%'
  AND SQL_TEXT NOT LIKE '%OPTSTAT%'
  AND SQL_TEXT NOT LIKE '%STATS$%'
  AND SQL_TEXT NOT LIKE '%DBSNMP%'
  AND SQL_TEXT NOT LIKE '%OUTLN%'
  AND SQL_TEXT NOT LIKE '%AUDIT%'
  AND PARSING_SCHEMA_NAME NOT IN ('SYS','SYSTEM','DBSNMP','OUTLN','MDSYS','CTXSYS','XDB','WMSYS')
"""

HEAVY_ELAPSED = f"""
SELECT SQL_ID,
       ROUND(ELAPSED_TIME/1000000, 2) AS ELAPSED_SEC,
       ROUND(CPU_TIME/1000000, 2) AS CPU_SEC,
       BUFFER_GETS, DISK_READS, EXECUTIONS, ROWS_PROCESSED,
       ROUND(ELAPSED_TIME/GREATEST(EXECUTIONS,1)/1000000, 3) AS AVG_SEC,
       SUBSTR(SQL_TEXT, 1, 300) AS SQL_TEXT
FROM V$SQL {_BIZ_WHERE} AND ELAPSED_TIME > 0
ORDER BY ELAPSED_TIME DESC FETCH FIRST 30 ROWS ONLY"""

HEAVY_AVG = f"""
SELECT SQL_ID,
       ROUND(ELAPSED_TIME/GREATEST(EXECUTIONS,1)/1000000, 3) AS AVG_SEC,
       ROUND(CPU_TIME/GREATEST(EXECUTIONS,1)/1000000, 3) AS AVG_CPU_SEC,
       ROUND(BUFFER_GETS/GREATEST(EXECUTIONS,1), 0) AS AVG_BUFFER,
       EXECUTIONS,
       ROUND(ELAPSED_TIME/1000000, 2) AS TOTAL_SEC,
       SUBSTR(SQL_TEXT, 1, 300) AS SQL_TEXT
FROM V$SQL {_BIZ_WHERE} AND EXECUTIONS >= 5 AND ELAPSED_TIME > 0
ORDER BY (ELAPSED_TIME/GREATEST(EXECUTIONS,1)) DESC FETCH FIRST 30 ROWS ONLY"""

HEAVY_BUFFER = f"""
SELECT SQL_ID,
       BUFFER_GETS, DISK_READS,
       ROUND(ELAPSED_TIME/1000000, 2) AS ELAPSED_SEC,
       EXECUTIONS,
       ROUND(BUFFER_GETS/GREATEST(EXECUTIONS,1), 0) AS AVG_BUFFER,
       SUBSTR(SQL_TEXT, 1, 300) AS SQL_TEXT
FROM V$SQL {_BIZ_WHERE} AND BUFFER_GETS > 0
ORDER BY BUFFER_GETS DESC FETCH FIRST 30 ROWS ONLY"""

HEAVY_DISK = f"""
SELECT SQL_ID,
       DISK_READS, BUFFER_GETS,
       ROUND(ELAPSED_TIME/1000000, 2) AS ELAPSED_SEC,
       EXECUTIONS,
       ROUND(DISK_READS/GREATEST(EXECUTIONS,1), 0) AS AVG_DISK,
       SUBSTR(SQL_TEXT, 1, 300) AS SQL_TEXT
FROM V$SQL {_BIZ_WHERE} AND DISK_READS > 0
ORDER BY DISK_READS DESC FETCH FIRST 30 ROWS ONLY"""

HEAVY_EXEC = f"""
SELECT SQL_ID,
       EXECUTIONS,
       ROUND(ELAPSED_TIME/1000000, 2) AS ELAPSED_SEC,
       BUFFER_GETS,
       ROUND(ELAPSED_TIME/GREATEST(EXECUTIONS,1)/1000000, 3) AS AVG_SEC,
       ROUND(BUFFER_GETS/GREATEST(EXECUTIONS,1), 0) AS AVG_BUFFER,
       SUBSTR(SQL_TEXT, 1, 300) AS SQL_TEXT
FROM V$SQL {_BIZ_WHERE}
ORDER BY EXECUTIONS DESC FETCH FIRST 30 ROWS ONLY"""

HEAVY_QUERIES = {
    "경과시간 TOP": HEAVY_ELAPSED,
    "평균시간 TOP": HEAVY_AVG,
    "버퍼 I/O TOP": HEAVY_BUFFER,
    "디스크 I/O TOP": HEAVY_DISK,
    "실행횟수 TOP": HEAVY_EXEC,
}

HEAVY_FULLTEXT = "SELECT SQL_FULLTEXT FROM V$SQL WHERE SQL_ID = '{}' FETCH FIRST 1 ROWS ONLY"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Gemini API
# ─────────────────────────────────────────────────────────────────────────────

def test_gemini_connection(api_key):
    if not HAS_GEMINI:
        return False, "실패함"
    try:
        client = genai.Client(api_key=api_key)
        client.models.generate_content(model="gemini-2.5-flash",
            contents="연결 테스트입니다. 'OK'라고만 답해주세요.")
        return True, "성공함"
    except Exception:
        return False, "실패함"

def call_gemini(api_key, prompt):
    if not HAS_GEMINI:
        raise RuntimeError("google-genai 미설치: pip install google-genai")
    client = genai.Client(api_key=api_key)
    return client.models.generate_content(
        model="gemini-2.5-flash", contents=prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=(
                "당신은 Oracle/Tibero SQL 튜닝 전문가입니다. "
                "인덱스 구조, 테이블 통계, 실행계획을 종합 분석하여 "
                "구체적이고 실행 가능한 튜닝 방안을 제시합니다. 한국어로 답변하세요."
            ),
            max_output_tokens=8192, temperature=0.3)).text


# ─────────────────────────────────────────────────────────────────────────────
# 4. 마크다운 렌더러
# ─────────────────────────────────────────────────────────────────────────────

class MarkdownRenderer:
    C = {"h1_bg":"#89b4fa","h1_fg":"#1e1e2e","h2_bg":"#313244","h2_fg":"#89b4fa",
         "h3_fg":"#a6e3a1","bold":"#f9e2af","code_bg":"#313244","code_fg":"#a6e3a1",
         "cb_bg":"#11111b","cb_fg":"#cba6f7","bullet":"#fab387","sep":"#45475a",
         "text":"#cdd6f4","dim":"#6c7086","th_bg":"#313244","tb":"#45475a"}

    def __init__(self, w):
        self.w = w; C = self.C
        w.tag_configure("h1", font=("Consolas",14,"bold"), background=C["h1_bg"],
            foreground=C["h1_fg"], spacing1=16, spacing3=10, lmargin1=10, rmargin=10)
        w.tag_configure("h2", font=("Consolas",12,"bold"), background=C["h2_bg"],
            foreground=C["h2_fg"], spacing1=14, spacing3=6, lmargin1=8, rmargin=8)
        w.tag_configure("h3", font=("Consolas",11,"bold"), foreground=C["h3_fg"],
            spacing1=10, spacing3=4)
        w.tag_configure("h4", font=("Consolas",10,"bold"), foreground=C["h3_fg"],
            spacing1=8, spacing3=4)
        w.tag_configure("body", font=("Consolas",10), foreground=C["text"],
            spacing1=2, spacing3=2, lmargin1=6, lmargin2=6, wrap="word")
        w.tag_configure("bold", font=("Consolas",10,"bold"), foreground=C["bold"])
        w.tag_configure("ic", font=("Consolas",10), background=C["code_bg"],
            foreground=C["code_fg"])
        w.tag_configure("cb", font=("Consolas",10), background=C["cb_bg"],
            foreground=C["cb_fg"], spacing1=2, spacing3=2, lmargin1=20,
            lmargin2=20, rmargin=20)
        w.tag_configure("cl", font=("Consolas",9,"italic"), background=C["cb_bg"],
            foreground=C["dim"], lmargin1=20, spacing1=6)
        w.tag_configure("bul", font=("Consolas",10), foreground=C["text"],
            lmargin1=20, lmargin2=34, spacing1=2, spacing3=2, wrap="word")
        w.tag_configure("bm", font=("Consolas",10), foreground=C["bullet"])
        w.tag_configure("b2", font=("Consolas",10), foreground=C["text"],
            lmargin1=36, lmargin2=50, spacing1=1, spacing3=1, wrap="word")
        w.tag_configure("b2m", font=("Consolas",10), foreground=C["dim"])
        w.tag_configure("num", font=("Consolas",10), foreground=C["text"],
            lmargin1=20, lmargin2=34, spacing1=3, spacing3=3, wrap="word")
        w.tag_configure("nm", font=("Consolas",10,"bold"), foreground=C["h2_fg"])
        w.tag_configure("sep", font=("Consolas",4), foreground=C["sep"],
            spacing1=8, spacing3=8)
        w.tag_configure("th", font=("Consolas",10,"bold"), background=C["th_bg"],
            foreground=C["h2_fg"], lmargin1=10, spacing1=4, spacing3=4)
        w.tag_configure("tr", font=("Consolas",10), foreground=C["text"],
            lmargin1=10, spacing1=2, spacing3=2)
        w.tag_configure("tb", font=("Consolas",10), foreground=C["tb"], lmargin1=10)

    def render(self, md):
        w = self.w; w.configure(state="normal"); w.delete("1.0","end")
        lines = md.split("\n"); i = 0
        while i < len(lines):
            ln = lines[i]
            if ln.strip().startswith("```"):
                lang = ln.strip()[3:].strip()
                if lang: w.insert("end", f"  ■ {lang}\n","cl")
                i += 1; code = []
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    code.append(lines[i]); i += 1
                if code: w.insert("end", "\n".join(code)+"\n","cb")
                i += 1; w.insert("end","\n"); continue
            if re.match(r'^\s*\|.*\|', ln):
                tbl = []
                while i < len(lines) and re.match(r'^\s*\|.*\|', lines[i]):
                    tbl.append(lines[i]); i += 1
                self._tbl(tbl); w.insert("end","\n"); continue
            if re.match(r'^-{3,}$|^\*{3,}$|^_{3,}$', ln.strip()):
                w.insert("end","─"*90+"\n","sep"); i+=1; continue
            for pfx, sz, tag in [("#### ",5,"h4"),("### ",4,"h3"),("## ",3,"h2"),("# ",2,"h1")]:
                if ln.startswith(pfx):
                    w.insert("end",f"  {ln[sz:].strip()}  \n",tag); i+=1; break
            else:
                m2 = re.match(r'^(\s{2,}|\t)[-*]\s+(.*)', ln)
                if m2: w.insert("end","  ◦ ","b2m"); self._r(m2.group(2),"b2"); w.insert("end","\n"); i+=1; continue
                m1 = re.match(r'^[-*]\s+(.*)', ln)
                if m1: w.insert("end"," ● ","bm"); self._r(m1.group(1),"bul"); w.insert("end","\n"); i+=1; continue
                mn = re.match(r'^(\d+)\.\s+(.*)', ln)
                if mn: w.insert("end",f" {mn.group(1)}. ","nm"); self._r(mn.group(2),"num"); w.insert("end","\n"); i+=1; continue
                if not ln.strip(): w.insert("end","\n"); i+=1; continue
                self._r(ln,"body"); w.insert("end","\n"); i+=1; continue
            continue
        w.configure(state="disabled")

    def _tbl(self, tlines):
        rows = []
        for t in tlines:
            cells = [c.strip() for c in t.strip().strip("|").split("|")]
            if not all(re.match(r'^[-:]+$',c) for c in cells): rows.append(cells)
        if not rows: return
        mc = max(len(r) for r in rows)
        ws = [0]*mc
        for r in rows:
            for i,c in enumerate(r):
                if i < mc: ws[i] = max(ws[i], len(c))
        def f(cs): return " │ ".join((cs[i] if i<len(cs) else "").ljust(ws[i]) for i in range(mc))
        self.w.insert("end","  "+f(rows[0])+"\n","th")
        self.w.insert("end","  "+"──┼──".join("─"*w for w in ws)+"\n","tb")
        for r in rows[1:]: self.w.insert("end","  "+f(r)+"\n","tr")

    def _r(self, text, tag):
        for p in re.split(r'(\*\*.*?\*\*|`[^`]+`)', text):
            if p.startswith("**") and p.endswith("**"): self.w.insert("end",p[2:-2],"bold")
            elif p.startswith("`") and p.endswith("`"): self.w.insert("end",f" {p[1:-1]} ","ic")
            else: self.w.insert("end",p,tag)


# ─────────────────────────────────────────────────────────────────────────────
# 5. GUI
# ─────────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SQL Tuning Advisor — Oracle + Gemini")
        self.geometry("1200x920")
        self.configure(bg="#1e1e2e")
        self._db = None
        self._raw_result = ""
        self._show_raw = False
        self._build_ui()

    def _build_ui(self):
        S = ttk.Style(self)
        S.theme_use("clam")
        S.configure("TNotebook", background="#1e1e2e", borderwidth=0)
        S.configure("TNotebook.Tab", background="#313244", foreground="#cdd6f4",
                     padding=[12, 6], font=("Consolas", 10))
        S.map("TNotebook.Tab", background=[("selected","#89b4fa")],
              foreground=[("selected","#1e1e2e")])
        S.configure("TFrame", background="#1e1e2e")
        S.configure("TLabel", background="#1e1e2e", foreground="#cdd6f4", font=("Consolas",10))
        S.configure("TEntry", fieldbackground="#313244", foreground="#cdd6f4", insertcolor="#cdd6f4")
        S.configure("TButton", background="#89b4fa", foreground="#1e1e2e",
                     font=("Consolas",10,"bold"), padding=[8,4])
        S.map("TButton", background=[("active","#b4befe")])
        S.configure("API.TButton", background="#a6e3a1", foreground="#1e1e2e",
                     font=("Consolas",10,"bold"), padding=[8,4])
        S.map("API.TButton", background=[("active","#94e2d5")])
        S.configure("Gemini.TButton", background="#cba6f7", foreground="#1e1e2e",
                     font=("Consolas",11,"bold"), padding=[14,6])
        S.map("Gemini.TButton", background=[("active","#f5c2e7")])
        S.configure("Heavy.TButton", background="#fab387", foreground="#1e1e2e",
                     font=("Consolas",10,"bold"), padding=[10,5])
        S.map("Heavy.TButton", background=[("active","#f9e2af")])
        # Treeview 스타일
        S.configure("Dark.Treeview", background="#181825", foreground="#cdd6f4",
                     fieldbackground="#181825", font=("Consolas",9), rowheight=22)
        S.configure("Dark.Treeview.Heading", background="#313244", foreground="#89b4fa",
                     font=("Consolas",9,"bold"))
        S.map("Dark.Treeview", background=[("selected","#45475a")],
              foreground=[("selected","#f9e2af")])

        # ══════════════════════════════════════════════════════════════════════
        # 상단 접속부 (버튼 간 칼정렬 대칭 레이아웃 - Password 너비 조정)
        # ══════════════════════════════════════════════════════════════════════
        # 외곽 카드 프레임
        conn_wrapper = tk.Frame(self, bg="#313244", bd=1, relief="flat")
        conn_wrapper.pack(fill="x", padx=16, pady=(16, 8))
        
        conn = ttk.Frame(conn_wrapper, padding=12)
        conn.pack(fill="x")
        
        # 스타일 테마 동기화
        S.configure("TFrame", background="#313244")
        S.configure("TLabel", background="#313244", foreground="#cdd6f4")

        # ── [1행] DB 접속 정보 (Password 칸을 늘려 버튼 라인 일치) ──
        row1 = ttk.Frame(conn)
        row1.pack(anchor="w", pady=(0, 6)) # 왼쪽(w) 기준 정렬

        ttk.Label(row1, text="Host :").pack(side="left", padx=(0, 2))
        self.e_host = ttk.Entry(row1, width=14)
        self.e_host.insert(0, "localhost")
        self.e_host.pack(side="left", padx=(0, 12))

        ttk.Label(row1, text="Port :").pack(side="left", padx=(0, 2))
        self.e_port = ttk.Entry(row1, width=5)
        self.e_port.insert(0, "1521")
        self.e_port.pack(side="left", padx=(0, 12))

        ttk.Label(row1, text="SID :").pack(side="left", padx=(0, 2))
        self.e_dbname = ttk.Entry(row1, width=8)
        self.e_dbname.insert(0, "XE")
        self.e_dbname.pack(side="left", padx=(0, 12))

        ttk.Label(row1, text="User :").pack(side="left", padx=(0, 2))
        self.e_user = ttk.Entry(row1, width=12)
        self.e_user.insert(0, "SYSTEM")
        self.e_user.pack(side="left", padx=(0, 12))

        ttk.Label(row1, text="Password :").pack(side="left", padx=(0, 2))
        # [수정] 너비를 12에서 14로 살짝 늘려 아래행 API Key 칸과 끝선을 동기화
        self.e_pw = ttk.Entry(row1, width=16, show="*")
        self.e_pw.pack(side="left", padx=(0, 14)) # 버튼 전 여백

        # 버튼과 상태값 정렬용 내부 프레임
        align_grid1 = ttk.Frame(row1)
        align_grid1.pack(side="left")

        # DB 접속 버튼 (이제 아래 API 검증 버튼과 시작/끝 라인이 칼같이 맞음)
        self.btn_connect = ttk.Button(align_grid1, text="DB 접속", command=self._connect, width=10)
        self.btn_connect.grid(row=0, column=0, padx=(0, 6))
        
        self.lbl_status = ttk.Label(align_grid1, text="● 미접속", foreground="#f38ba8", font=("Consolas", 10, "bold"))
        self.lbl_status.grid(row=0, column=1, sticky="w")


        # ── [2행] 환경 설정 ──
        row2 = ttk.Frame(conn)
        row2.pack(anchor="w", pady=(6, 0)) # 왼쪽(w) 기준 정렬

        # JDBC JAR 영역
        ttk.Label(row2, text="JDBC JAR :").pack(side="left", padx=(0, 2))
        self.e_jar = ttk.Entry(row2, width=24) 
        self.e_jar.insert(0, DEFAULT_JAR)
        self.e_jar.pack(side="left", padx=(0, 2))
        
        btn_browse = ttk.Button(row2, text="...", width=3, command=self._browse_jar)
        btn_browse.pack(side="left", padx=(0, 16))

        # Gemini API Key 영역
        ttk.Label(row2, text="API Key :", foreground="#cba6f7", font=("Consolas", 10, "bold")).pack(side="left", padx=(0, 2))
        self.e_apikey = ttk.Entry(row2, show="*", width=40) 
        self.e_apikey.pack(side="left", padx=(0, 14))

        # 버튼과 상태값 정렬용 내부 프레임
        align_grid2 = ttk.Frame(row2)
        align_grid2.pack(side="left")

        # API 검증 버튼
        self.btn_test_api = ttk.Button(align_grid2, text="API 검증", style="API.TButton", command=self._test_api, width=10)
        self.btn_test_api.grid(row=0, column=0, padx=(0, 6))
        
        self.lbl_api_status = ttk.Label(align_grid2, text="", foreground="#6c7086", font=("Consolas", 9, "italic"))
        self.lbl_api_status.grid(row=0, column=1, sticky="w")
        # ══════════════════════════════════════════════════════════════════════
        # 탭
        # ══════════════════════════════════════════════════════════════════════
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=(6,4))
        self.notebook = nb

        self.tab_heavy  = ttk.Frame(nb, padding=6)
        self.tab_input  = ttk.Frame(nb, padding=6)
        self.tab_index  = ttk.Frame(nb, padding=6)
        self.tab_vsql   = ttk.Frame(nb, padding=6)
        self.tab_prompt = ttk.Frame(nb, padding=6)
        self.tab_result = ttk.Frame(nb, padding=6)

        nb.add(self.tab_heavy,  text="① SQL 분석")
        nb.add(self.tab_input,  text="② SQL 입력")
        nb.add(self.tab_index,  text="③ 인덱스/통계 조회")
        nb.add(self.tab_vsql,   text="④ 실행계획 추적")
        nb.add(self.tab_prompt, text="⑤ 프롬프트 생성")
        nb.add(self.tab_result, text="⑥ 개선방안 요청")

        self._build_tab_heavy()
        self._build_tab_input()
        self._build_tab_index()
        self._build_tab_vsql()
        self._build_tab_prompt()
        self._build_tab_result()

        # 하단 로그
        lf = ttk.Frame(self, padding=(12,0,12,8)); lf.pack(fill="x")
        ttk.Label(lf, text="로그").pack(anchor="w")
        self.log_box = scrolledtext.ScrolledText(
            lf, height=6, bg="#181825", fg="#a6e3a1",
            font=("Consolas",9), state="disabled")
        self.log_box.pack(fill="x")

    # ══════════════════════════════════════════════════════════════════════════
    # 탭 빌드
    # ══════════════════════════════════════════════════════════════════════════

    def _build_tab_heavy(self):
        """① SQL 분석 - Treeview 표 + SQL 전문 조회."""
        f = self.tab_heavy

        # 상단 조건 (조회 버튼을 정렬 기준 바로 옆 1열로 통합)
        top = tk.Frame(f, bg="#313244", padx=12, pady=8)
        top.pack(fill="x", pady=(0,6))

        row1 = tk.Frame(top, bg="#313244")
        row1.pack(fill="x")
        
        tk.Label(row1, text="정렬 기준", bg="#313244", fg="#89b4fa",
                 font=("Consolas",10,"bold")).pack(side="left", padx=(0,12))
        
        self.heavy_sort = tk.StringVar(value="경과시간 TOP")
        for lbl in HEAVY_QUERIES:
            tk.Radiobutton(row1, text=lbl, variable=self.heavy_sort, value=lbl,
                           bg="#313244", fg="#cdd6f4", selectcolor="#45475a",
                           activebackground="#313244", activeforeground="#f9e2af",
                           font=("Consolas",9)).pack(side="left", padx=5)

        # 조회 버튼
        self.btn_heavy = ttk.Button(row1, text="🔍 조회", command=self._run_heavy_query)
        self.btn_heavy.pack(side="left", padx=(16, 0))

        # 🚀 [수정/추가] 에러가 나던 상태 표시 라벨을 여기에 생성합니다.
        # ttk가 아닌 오리지널 tk.Label을 써야 fg 옵션(Catppuccin 테마색)이 먹힙니다.
        self.lbl_heavy_status = tk.Label(row1, text="", bg="#313244", fg="#a6e3a1",
                                         font=("Consolas", 10, "bold"))
        self.lbl_heavy_status.pack(side="left", padx=(8, 0))

        # 안내 메시지만 row1 우측 끝에 정렬
        tk.Label(row1, text="※ TB_ 업무 테이블 SQL만 조회", bg="#313244", fg="#6c7086",
                 font=("Consolas",8)).pack(side="right", padx=4)

        # Treeview (표)
        tree_frame = ttk.Frame(f)
        tree_frame.pack(fill="both", expand=True, pady=(0,6))

        self.heavy_tree = ttk.Treeview(tree_frame, style="Dark.Treeview",
                                        show="headings", selectmode="browse")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.heavy_tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.heavy_tree.xview)
        self.heavy_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        self.heavy_tree.pack(fill="both", expand=True)
        
        # 더블클릭으로 SQL_ID 자동 입력
        self.heavy_tree.bind("<Double-1>", self._on_tree_dblclick)

        # 하단: SQL 전문 조회
        bot = tk.Frame(f, bg="#313244", padx=12, pady=8)
        bot.pack(fill="x", pady=(0,0))
        brow = tk.Frame(bot, bg="#313244"); brow.pack(fill="x")
        tk.Label(brow, text="SQL_ID:", bg="#313244", fg="#fab387",
                 font=("Consolas",10,"bold")).pack(side="left")
        self.e_heavy_sqlid = ttk.Entry(brow, width=18)
        self.e_heavy_sqlid.pack(side="left", padx=(4,8))
        ttk.Button(brow, text="SQL 전문 조회", command=self._run_heavy_fulltext).pack(side="left", padx=4)
        ttk.Button(brow, text="→ ② SQL 입력 탭으로", command=self._send_to_input).pack(side="left", padx=4)

        self.heavy_fulltext_box = scrolledtext.ScrolledText(
            f, height=5, bg="#11111b", fg="#cba6f7",
            font=("Consolas",10), state="disabled", wrap="word")
        self.heavy_fulltext_box.pack(fill="x", pady=(4,0))

    def _build_tab_input(self):
        f = self.tab_input
        ttk.Label(f, text="SQL을 입력하세요:").pack(anchor="w")
        self.sql_input = scrolledtext.ScrolledText(
            f, height=12, bg="#313244", fg="#cdd6f4",
            font=("Consolas",11), insertbackground="#cdd6f4")
        self.sql_input.pack(fill="both", expand=True)
        b = ttk.Frame(f); b.pack(fill="x", pady=6)
        ttk.Button(b, text="테이블 파싱 →", command=self._parse_tables).pack(side="left", padx=4)
        ttk.Label(f, text="파싱된 테이블:").pack(anchor="w")
        self.lbl_tables = ttk.Label(f, text="(없음)", foreground="#89b4fa")
        self.lbl_tables.pack(anchor="w")

    def _build_tab_index(self):
        f = self.tab_index
        ttk.Label(f, text="자동 생성된 인덱스 조회 쿼리:").pack(anchor="w")
        self.index_query_box = scrolledtext.ScrolledText(
            f, height=5, bg="#313244", fg="#fab387", font=("Consolas",10), state="disabled")
        self.index_query_box.pack(fill="x")
        b = ttk.Frame(f); b.pack(fill="x", pady=6)
        ttk.Button(b, text="★ 일괄 조회", command=self._run_all_stats).pack(side="left", padx=(0,12))
        ttk.Button(b, text="인덱스 조회", command=self._run_index_query).pack(side="left", padx=4)
        ttk.Button(b, text="테이블 통계", command=self._run_table_stats).pack(side="left", padx=4)
        ttk.Button(b, text="인덱스 통계", command=self._run_index_stats).pack(side="left", padx=4)
        nb2 = ttk.Notebook(f); nb2.pack(fill="both", expand=True)
        for name, attr in [("인덱스 구조","index_result_box"),("테이블 통계","table_stats_box"),
                           ("인덱스 통계","index_stats_box")]:
            tab = ttk.Frame(nb2, padding=4); nb2.add(tab, text=name)
            w = scrolledtext.ScrolledText(tab, bg="#181825", fg="#cdd6f4",
                                          font=("Consolas",10), state="disabled")
            w.pack(fill="both", expand=True); setattr(self, attr, w)

    def _build_tab_vsql(self):
        f = self.tab_vsql
        ttk.Label(f, text="채번 태그 삽입 SQL (미리보기):").pack(anchor="w")
        self.tagged_sql_box = scrolledtext.ScrolledText(
            f, height=8, bg="#313244", fg="#a6e3a1", font=("Consolas",10), state="disabled")
        self.tagged_sql_box.pack(fill="x")
        b = ttk.Frame(f); b.pack(fill="x", pady=6)
        self.btn_run_trace = ttk.Button(b, text="SQL 실행 + V$SQL 추적", command=self._run_and_trace)
        self.btn_run_trace.pack(side="left", padx=4)
        self.lbl_trace_status = ttk.Label(b, text="", foreground="#fab387")
        self.lbl_trace_status.pack(side="left", padx=12)
        ttk.Label(f, text="V$SQL 결과 / 실행계획:").pack(anchor="w")
        self.vsql_result_box = scrolledtext.ScrolledText(
            f, height=14, bg="#181825", fg="#cdd6f4", font=("Consolas",10), state="disabled")
        self.vsql_result_box.pack(fill="both", expand=True)

    def _build_tab_prompt(self):
        f = self.tab_prompt
        b = ttk.Frame(f); b.pack(fill="x", pady=(0,8))
        ttk.Button(b, text="▶  프롬프트 생성", command=self._build_prompt).pack(side="left", padx=4)
        ttk.Button(b, text="📋  클립보드 복사", command=self._copy_prompt).pack(side="left", padx=4)
        self.lbl_prompt_status = ttk.Label(b, text="", foreground="#a6e3a1")
        self.lbl_prompt_status.pack(side="left", padx=8)
        ttk.Label(f, text="↓ 자동 생성된 프롬프트 (편집 가능)", foreground="#fab387").pack(anchor="w")
        self.prompt_box = scrolledtext.ScrolledText(
            f, bg="#181825", fg="#cdd6f4", font=("Consolas",10), wrap="word")
        self.prompt_box.pack(fill="both", expand=True)

    def _build_tab_result(self):
        f = self.tab_result
        b = ttk.Frame(f); b.pack(fill="x", pady=(0,8))
        self.btn_gemini_call = ttk.Button(b, text="  🚀  Gemini 분석 요청  ",
            style="Gemini.TButton", command=self._call_gemini_api)
        self.btn_gemini_call.pack(side="left", padx=4)
        self.lbl_gemini_status = ttk.Label(b, text="", foreground="#cdd6f4")
        self.lbl_gemini_status.pack(side="left", padx=12)
        ttk.Button(b, text="📋 결과 복사", command=self._copy_result).pack(side="right", padx=4)
        ttk.Button(b, text="Raw 보기", command=self._toggle_raw).pack(side="right", padx=4)
        rf = ttk.Frame(f); rf.pack(fill="both", expand=True)
        self.result_box = tk.Text(rf, bg="#1e1e2e", fg="#cdd6f4", font=("Consolas",10),
            wrap="word", state="disabled", padx=12, pady=12, relief="flat", borderwidth=0,
            selectbackground="#45475a", selectforeground="#cdd6f4")
        sb = ttk.Scrollbar(rf, orient="vertical", command=self.result_box.yview)
        self.result_box.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.result_box.pack(fill="both", expand=True)
        self.md_renderer = MarkdownRenderer(self.result_box)

    # ══════════════════════════════════════════════════════════════════════════
    # 헬퍼
    # ══════════════════════════════════════════════════════════════════════════

    def _log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg+"\n"); self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_text(self, w, text):
        w.configure(state="normal"); w.delete("1.0","end")
        w.insert("1.0", text); w.configure(state="disabled")

    def _rows_to_text(self, cols, rows):
        if not rows: return "(조회 결과 없음)"
        ws = [max(len(str(c)), max((len(str(r[i])) for r in rows), default=0))
              for i, c in enumerate(cols)]
        def fmt(r): return "  ".join(str(v).ljust(ws[i]) for i, v in enumerate(r))
        return "\n".join([fmt(cols), "  ".join("-"*w for w in ws)] + [fmt(r) for r in rows])

    # ══════════════════════════════════════════════════════════════════════════
    # 이벤트 핸들러
    # ══════════════════════════════════════════════════════════════════════════

    def _browse_jar(self):
        p = filedialog.askopenfilename(title="JDBC JAR", filetypes=[("JAR","*.jar"),("All","*.*")])
        if p: self.e_jar.delete(0,"end"); self.e_jar.insert(0, p)

    def _connect(self):
        h,p,d = self.e_host.get().strip(), self.e_port.get().strip(), self.e_dbname.get().strip()
        u,pw,jar = self.e_user.get().strip(), self.e_pw.get().strip(), self.e_jar.get().strip()
        if not h or not u or not d:
            messagebox.showwarning("입력 오류", "Host, SID, User 입력 필요"); return
        if not HAS_JDBC:
            messagebox.showerror("설치 오류", "jaydebeapi 미설치"); return
        try:
            self._db = OracleConnection(h,p,d,u,pw,jar).connect()
            self.lbl_status.configure(text="● 접속됨", foreground="#a6e3a1")
            self._log(f"[DB] 접속 성공: {h}:{p}/{d}")
        except Exception as e:
            self._log(f"[ERROR] {e}"); messagebox.showerror("접속 실패", str(e))

    def _test_api(self):
        key = self.e_apikey.get().strip()
        if not key: messagebox.showwarning("입력 없음", "API Key 필요"); return
        self.lbl_api_status.configure(text="⏳", foreground="#fab387")
        self.btn_test_api.configure(state="disabled")
        def _run():
            ok, msg = test_gemini_connection(key)
            def _u():
                self.lbl_api_status.configure(text=msg, foreground="#a6e3a1" if ok else "#f38ba8")
                self.btn_test_api.configure(state="normal")
                self._log(f"[API] {msg}")
            self.after(0, _u)
        threading.Thread(target=_run, daemon=True).start()

    def _parse_tables(self):
        sql = self.sql_input.get("1.0","end").strip()
        if not sql: messagebox.showwarning("입력 없음", "SQL 필요"); return
        tables = extract_tables(sql)
        self.lbl_tables.configure(text=", ".join(tables) if tables else "(없음)")
        self._log(f"[PARSE] {tables}")
        if tables: self._set_text(self.index_query_box, build_index_query(tables))

    def _run_index_query(self):
        q = self.index_query_box.get("1.0","end").strip()
        if not q: messagebox.showwarning("없음", "테이블 파싱 먼저"); return
        if not self._db: messagebox.showwarning("미접속", "DB 접속 필요"); return
        try:
            c, r = self._db.execute(q)
            t = "\n".join(["\t".join(c)]+["\t".join(str(v) for v in row) for row in r])
            self._set_text(self.index_result_box, t); self._index_info = t
            self._log(f"[DB] 인덱스 {len(r)}건")
        except Exception as e: self._log(f"[ERROR] {e}"); messagebox.showerror("오류", str(e))

    def _get_tables(self):
        t = extract_tables(self.sql_input.get("1.0","end").strip())
        if not t: messagebox.showwarning("없음", "테이블 파싱 먼저")
        return t

    def _run_table_stats(self):
        t = self._get_tables()
        if not t: return
        if not self._db: messagebox.showwarning("미접속","DB 접속 필요"); return
        try:
            c, r = self._db.execute(build_table_stats_query(t))
            txt = self._rows_to_text(c, r)
            self._set_text(self.table_stats_box, txt); self._table_stats = txt
        except Exception as e: self._log(f"[ERROR] {e}"); messagebox.showerror("오류", str(e))

    def _run_index_stats(self):
        t = self._get_tables()
        if not t: return
        if not self._db: messagebox.showwarning("미접속","DB 접속 필요"); return
        try:
            c, r = self._db.execute(build_index_stats_query(t))
            txt = self._rows_to_text(c, r)
            self._set_text(self.index_stats_box, txt); self._index_stats = txt
        except Exception as e: self._log(f"[ERROR] {e}"); messagebox.showerror("오류", str(e))

    def _run_all_stats(self):
        self._run_index_query(); self._run_table_stats(); self._run_index_stats()

    def _run_and_trace(self):
        sql = self.sql_input.get("1.0","end").strip()
        if not sql: messagebox.showwarning("입력 없음","SQL 필요"); return
        tag = str(uuid.uuid4())[:8].upper()
        tagged = inject_comment(sql, tag)
        self._set_text(self.tagged_sql_box, tagged)
        self._tag = tag; self._log(f"[TAG] {tag}")
        if not self._db: messagebox.showwarning("미접속","DB 접속 필요"); return
        def _run():
            try:
                self.after(0, lambda: self.btn_run_trace.configure(state="disabled"))
                self.after(0, lambda: self.lbl_trace_status.configure(text="⏳ 실행 중..."))
                self._db.execute_no_fetch(tagged)
                self.after(0, lambda: self.lbl_trace_status.configure(text="⏳ V$SQL..."))
                c, r = self._db.execute(build_vsql_query(tag))
                lines = ["\t".join(c)]+["\t".join(str(v) for v in row) for row in r]
                exp = ""
                if r:
                    sid, cn = r[0][0], r[0][1]
                    ec, er = self._db.execute(build_explain_query(sid, cn))
                    exp = "\n".join(["\t".join(ec)]+["\t".join(str(v) for v in row) for row in er])
                    self._log(f"[DB] SQL_ID={sid}")
                    self.after(0, lambda s=sid: self.lbl_trace_status.configure(text=f"✓ SQL_ID={s}"))
                else:
                    self.after(0, lambda: self.lbl_trace_status.configure(text="⚠ SQL_ID 없음"))
                self._explain_plan = exp
                self.after(0, lambda: self._set_text(self.vsql_result_box,
                    f"[V$SQL]\n{chr(10).join(lines)}\n\n[실행계획]\n{exp}"))
                self.after(0, lambda: self.btn_run_trace.configure(state="normal"))
            except Exception as e:
                self._log(f"[ERROR] {e}")
                self.after(0, lambda: self.lbl_trace_status.configure(text="✗ 실패"))
                self.after(0, lambda: self.btn_run_trace.configure(state="normal"))
        threading.Thread(target=_run, daemon=True).start()

    # ── ⑤ 프롬프트 ──
    def _build_prompt(self):
        sql = self.sql_input.get("1.0","end").strip()
        idx = getattr(self,"_index_info","(③ 탭에서 먼저 실행)")
        tst = getattr(self,"_table_stats","(없음)")
        ist = getattr(self,"_index_stats","(없음)")
        exp = getattr(self,"_explain_plan","(④ 탭에서 먼저 실행)")
        self.prompt_box.delete("1.0","end")
        self.prompt_box.insert("1.0", f"""아래 SQL과 실행 환경 정보를 분석해서 구체적인 튜닝 방안을 제시해줘.

## 원본 SQL
```sql
{sql}
```

## 인덱스 구조 (DBA_INDEXES / DBA_IND_COLUMNS)
```
{idx}
```

## 테이블 통계 (DBA_TABLES)
```
{tst}
```

## 인덱스 통계 (BLEVEL/DISTINCT_KEYS/CLUSTERING_FACTOR)
```
{ist}
```

## 실행계획 (DBMS_XPLAN.DISPLAY_CURSOR)
```
{exp}
```

아래 항목별로 답변해줘:
1. **문제점 분석** - 현재 SQL/인덱스의 비효율 원인 (통계 기반으로)
2. **인덱스 개선안** - 추가/변경 권장 인덱스 (CREATE INDEX 구문 포함)
3. **통계 이슈** - 통계가 오래됐거나 잘못된 경우 ANALYZE 권고
4. **SQL 재작성안** - 개선된 SQL (있다면)
5. **예상 효과** - 개선 후 기대되는 성능 향상
""")
        self.lbl_prompt_status.configure(text="✓ 생성 완료")
        self.after(5000, lambda: self.lbl_prompt_status.configure(text=""))

    def _copy_prompt(self):
        t = self.prompt_box.get("1.0","end").strip()
        if not t: return
        self.clipboard_clear(); self.clipboard_append(t)
        self.lbl_prompt_status.configure(text="✓ 복사!")
        self.after(3000, lambda: self.lbl_prompt_status.configure(text=""))

    # ── ⑥ Gemini ──
    def _call_gemini_api(self):
        key = self.e_apikey.get().strip()
        if not key: messagebox.showwarning("없음","API Key 필요"); return
        prompt = self.prompt_box.get("1.0","end").strip()
        if not prompt: self._build_prompt(); prompt = self.prompt_box.get("1.0","end").strip()
        if not prompt: return
        self.btn_gemini_call.configure(state="disabled")
        self.lbl_gemini_status.configure(text="⏳ 분석 중...", foreground="#fab387")
        self.result_box.configure(state="normal"); self.result_box.delete("1.0","end")
        self.result_box.insert("1.0","\n\n    ⏳ Gemini API 응답 대기 중...")
        self.result_box.configure(state="disabled")
        self.notebook.select(self.tab_result)
        def _run():
            try:
                result = call_gemini(key, prompt); self._raw_result = result
                def _u():
                    self._show_raw = False; self.md_renderer.render(result)
                    self.lbl_gemini_status.configure(text="✓ 분석 완료!", foreground="#a6e3a1")
                    self.btn_gemini_call.configure(state="normal")
                self.after(0, _u)
            except Exception as e:
                def _e():
                    self.lbl_gemini_status.configure(text=f"✗ {str(e)[:60]}", foreground="#f38ba8")
                    self.btn_gemini_call.configure(state="normal")
                    messagebox.showerror("API 오류", str(e))
                self.after(0, _e)
        threading.Thread(target=_run, daemon=True).start()

    def _toggle_raw(self):
        if not self._raw_result: return
        self._show_raw = not self._show_raw
        if self._show_raw:
            self.result_box.configure(state="normal"); self.result_box.delete("1.0","end")
            self.result_box.insert("1.0", self._raw_result)
            self.result_box.configure(state="disabled")
        else: self.md_renderer.render(self._raw_result)

    def _copy_result(self):
        if not self._raw_result: return
        self.clipboard_clear(); self.clipboard_append(self._raw_result)
        self.lbl_gemini_status.configure(text="✓ 복사!", foreground="#a6e3a1")
        self.after(3000, lambda: self.lbl_gemini_status.configure(text=""))

    # ── ① SQL 분석 ──

    def _run_heavy_query(self):
        if not self._db: messagebox.showwarning("미접속","DB 접속 필요"); return
        sort_key = self.heavy_sort.get()
        query = HEAVY_QUERIES.get(sort_key, HEAVY_ELAPSED)
        self.btn_heavy.configure(state="disabled")
        self.lbl_heavy_status.configure(text="⏳ 조회 중...", fg="#fab387")
        self._log(f"[분석] {sort_key} 조회")

        def _run():
            try:
                cols, rows = self._db.execute(query)
                def _u():
                    self._populate_tree(cols, rows)
                    self.lbl_heavy_status.configure(text=f"✓ {len(rows)}건", fg="#a6e3a1")
                    self.btn_heavy.configure(state="normal")
                    self._log(f"[분석] {len(rows)}건")
                self.after(0, _u)
            except Exception as e:
                def _e():
                    self.lbl_heavy_status.configure(text="✗ 오류", fg="#f38ba8")
                    self.btn_heavy.configure(state="normal")
                    self._log(f"[ERROR] {e}"); messagebox.showerror("오류", str(e))
                self.after(0, _e)
        threading.Thread(target=_run, daemon=True).start()

    def _populate_tree(self, cols, rows):
        """Treeview에 결과를 표 형태로 채움."""
        tree = self.heavy_tree

        # 기존 데이터 삭제
        tree.delete(*tree.get_children())

        # SQL_TEXT 분리 (별도 표시하기엔 너무 길어서 마지막 컬럼으로)
        display_cols = [c for c in cols]
        tree["columns"] = [f"c{i}" for i in range(len(display_cols))]

        for i, col_name in enumerate(display_cols):
            cid = f"c{i}"
            # SQL_TEXT는 넓게, 나머지는 내용에 맞게
            if col_name == "SQL_TEXT":
                tree.heading(cid, text="SQL_TEXT", anchor="w")
                tree.column(cid, width=500, minwidth=200, anchor="w")
            elif col_name == "SQL_ID":
                tree.heading(cid, text="SQL_ID", anchor="w")
                tree.column(cid, width=110, minwidth=90, anchor="w")
            else:
                tree.heading(cid, text=col_name, anchor="e")
                # 숫자 컬럼 너비 추정
                w = max(len(col_name), 8) * 9 + 10
                tree.column(cid, width=min(w, 120), minwidth=60, anchor="e")

        # 데이터 삽입 + 홀짝행 태그
        tree.tag_configure("odd", background="#1a1a2e")
        tree.tag_configure("even", background="#181825")

        for idx, row in enumerate(rows):
            vals = []
            for v in row:
                s = str(v) if v is not None else ""
                if "SQL_TEXT" in cols and row.index(v) == cols.index("SQL_TEXT") if "SQL_TEXT" in cols else False:
                    s = s.replace("\n", " ")[:150]
                vals.append(s)
            # SQL_TEXT 줄바꿈 제거 + 자르기
            for ci, cn in enumerate(cols):
                if cn == "SQL_TEXT" and ci < len(vals):
                    vals[ci] = vals[ci].replace("\n", " ")[:150]
            tag = "odd" if idx % 2 else "even"
            tree.insert("", "end", values=vals, tags=(tag,))

        # 저장 (복사용)
        self._heavy_cols = cols
        self._heavy_rows = rows

    def _on_tree_dblclick(self, event):
        """Treeview 행 더블클릭 → SQL_ID 자동 입력."""
        sel = self.heavy_tree.selection()
        if not sel: return
        vals = self.heavy_tree.item(sel[0], "values")
        if vals:
            sql_id = vals[0]  # 첫 번째 컬럼이 SQL_ID
            self.e_heavy_sqlid.delete(0, "end")
            self.e_heavy_sqlid.insert(0, sql_id)

    def _run_heavy_fulltext(self):
        if not self._db: messagebox.showwarning("미접속","DB 접속 필요"); return
        sid = self.e_heavy_sqlid.get().strip()
        if not sid: messagebox.showwarning("입력 없음","SQL_ID 입력"); return
        try:
            c, r = self._db.execute(HEAVY_FULLTEXT.format(sid))
            if r:
                clob_obj = r[0][0]
                sql_text = ""
                
                if clob_obj is not None:
                    # stringValue() 메서드가 존재하면 바로 호출하여 문자열 추출
                    if hasattr(clob_obj, "stringValue"):
                        sql_text = clob_obj.stringValue()
                    elif hasattr(clob_obj, "getSubString"):
                        # 만약 stringValue가 안 먹히면 순서와 타입을 명확히 해서 호출
                        clob_len = int(clob_obj.length())
                        sql_text = clob_obj.getSubString(1, clob_len)
                    else:
                        sql_text = str(clob_obj)
                
                self._set_text(self.heavy_fulltext_box, sql_text)
                self._log(f"[분석] SQL_ID={sid} 전문")
            else:
                self._set_text(self.heavy_fulltext_box, f"(SQL_ID={sid} 없음)")
        except Exception as e:
            self._log(f"[ERROR] {e}"); messagebox.showerror("오류", str(e))

    def _send_to_input(self):
        t = self.heavy_fulltext_box.get("1.0","end").strip()
        if not t or t.startswith("("): messagebox.showwarning("없음","SQL 전문 조회 먼저"); return
        self.sql_input.delete("1.0","end"); self.sql_input.insert("1.0", t)
        self.notebook.select(self.tab_input)
        self._log("[분석] SQL → ② SQL 입력 탭")

    def _copy_heavy(self):
        cols = getattr(self, "_heavy_cols", None)
        rows = getattr(self, "_heavy_rows", None)
        if not cols or not rows: return
        text = self._rows_to_text(cols, rows)
        self.clipboard_clear(); self.clipboard_append(text)
        self.lbl_heavy_status.configure(text="✓ 복사!", fg="#a6e3a1")
        self.after(3000, lambda: self.lbl_heavy_status.configure(text=""))


if __name__ == "__main__":
    App().mainloop()
