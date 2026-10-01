import os
from dotenv import load_dotenv
import datetime
import traceback
import sqlite3
import hashlib

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_URL = os.getenv("DATABASE_URL")

# Backend flags
HAS_MONGO = bool(MONGODB_URI)
HAS_POSTGRES = bool(DATABASE_URL)

# Connection clients
mongo_client = None
mongo_db = None
admins_col = None
searches_col = None
results_col = None
passwords_col = None

if HAS_MONGO:
    try:
        from pymongo import MongoClient
        from pymongo.errors import DuplicateKeyError, OperationFailure, BulkWriteError
        print("🍃 MongoDB Configured")
        mongo_client = MongoClient(MONGODB_URI)
        try:
            mongo_db = mongo_client.get_default_database()
        except Exception:
            mongo_db = mongo_client['search_bot']
        admins_col = mongo_db['admins']
        searches_col = mongo_db['searches']
        results_col = mongo_db['results']
        passwords_col = mongo_db['passwords']
        passwords_col.create_index("password", unique=True)
        admins_col.create_index("user_id", unique=True)
        # Dedup on a 40-byte hash instead of (keyword, result_line): avoids Mongo's
        # 1024-byte index-key limit blowing up on long lines. sparse=True so pre-upgrade
        # docs without the field don't collide on null while the new index builds.
        results_col.create_index("line_hash", unique=True, sparse=True)
        results_col.create_index("keyword")
        try:
            results_col.drop_index("keyword_1_result_line_1")
        except Exception:
            pass  # old heavy index may not exist
    except Exception as e:
        print(f"⚠️ MongoDB Init Error: {e}")
        HAS_MONGO = False

pg_pool = None
if HAS_POSTGRES:
    try:
        import psycopg2
        import psycopg2.extras
        from psycopg2.pool import SimpleConnectionPool
        print("🐘 PostgreSQL Configured")
        # connect_timeout: don't hang startup forever if the Postgres server is down/unreachable
        pg_pool = SimpleConnectionPool(1, 10, DATABASE_URL, connect_timeout=10)
        
        # Init tables
        conn = pg_pool.getconn()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS admins (user_id BIGINT PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS zip_passwords (password TEXT PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS searches (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            keywords TEXT,
            source TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS results (
            id SERIAL PRIMARY KEY,
            search_id TEXT,
            keyword TEXT,
            result_line TEXT,
            line_hash TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        # Migration + small dedup index (replaces the heavy unique on long text).
        c.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS line_hash TEXT")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_results_hash ON results(line_hash)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_results_keyword ON results(keyword)")
        conn.commit()
        c.close()
        pg_pool.putconn(conn)
    except Exception as e:
        print(f"⚠️ PostgreSQL Init Error: {e}")
        HAS_POSTGRES = False

# DB_FILE can be set directly, or DATA_DIR can point at a mounted persistent volume
# (e.g. Coolify/Docker volume at /app/data) so the SQLite file survives redeploys.
DB_FILE = os.getenv("DB_FILE") or os.path.join(os.getenv("DATA_DIR", os.path.dirname(__file__)), "bot_data.db")
try:
    os.makedirs(os.path.dirname(os.path.abspath(DB_FILE)), exist_ok=True)
except Exception:
    pass

def _get_sqlite_conn():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL = readers don't block the writer -> far fewer "database is locked" errors
    # under the bot's concurrent batch writes. journal_mode is persistent; the rest
    # are per-connection and cheap to set each open.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA cache_size=-65536")  # ~64 MB page cache
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn

def _line_hash(keyword, line):
    """Fixed-size dedup key: a small 40-byte index instead of indexing long text.
    Avoids the slow/oversized unique index on result_line (and Mongo's 1024-byte
    index-key limit on long lines)."""
    return hashlib.sha1(f"{keyword}\x00{line}".encode("utf-8", "ignore")).hexdigest()

def init_sqlite():
    conn = _get_sqlite_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS zip_passwords (password TEXT PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        keywords TEXT,
        source TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        search_id TEXT,
        keyword TEXT,
        result_line TEXT,
        line_hash TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    # Migration for DBs created before the line_hash column existed.
    try:
        c.execute("ALTER TABLE results ADD COLUMN line_hash TEXT")
    except sqlite3.OperationalError:
        pass  # column already present
    # Dedup now rides on this small hash index (NULLs from old rows stay distinct).
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_results_hash ON results(line_hash)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_results_keyword ON results(keyword)")
    conn.commit()
    conn.close()

init_sqlite()

# ----- Backend Switcher -----
ACTIVE_BACKEND = "sqlite"
if HAS_MONGO:
    ACTIVE_BACKEND = "mongo"
elif HAS_POSTGRES:
    ACTIVE_BACKEND = "postgres"

def set_backend(name):
    global ACTIVE_BACKEND
    ok = False
    if name == "mongo" and HAS_MONGO: ACTIVE_BACKEND = "mongo"; ok = True
    elif name == "postgres" and HAS_POSTGRES: ACTIVE_BACKEND = "postgres"; ok = True
    elif name == "sqlite": ACTIVE_BACKEND = "sqlite"; ok = True
    if ok:
        _invalidate_admins_cache()  # different backend may have a different admin set
        _invalidate_passwords_cache()
    return ok

def get_backend():
    return ACTIVE_BACKEND

# --- Space Optimization Hook ---
def clean_old_searches(days=14):
    """Deletes search history older than N days to save space. Results are preserved."""
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    if ACTIVE_BACKEND == "mongo" and HAS_MONGO:
        try:
            searches_col.delete_many({"timestamp": {"$lt": cutoff}})
        except: pass
    elif ACTIVE_BACKEND == "postgres" and HAS_POSTGRES:
        try:
            conn = pg_pool.getconn()
            c = conn.cursor()
            c.execute("DELETE FROM searches WHERE timestamp < %s", (cutoff,))
            conn.commit()
            c.close()
            pg_pool.putconn(conn)
        except:
            pass
    else:
        try:
            conn = _get_sqlite_conn()
            c = conn.cursor()
            c.execute("DELETE FROM searches WHERE timestamp < ?", (cutoff,))
            conn.commit()
            conn.close()
        except:
            pass

# --- Auto Fallback Helper ---
def handle_mongo_quota_error(e):
    # AtlasError 8000: over space quota
    err_str = str(e).lower()
    if 'quota' in err_str or (hasattr(e, 'code') and e.code == 8000) or 'operationfailure' in err_str:
        print("⚠️ MongoDB QUOTA EXCEEDED! Auto-switching to Postgres/SQLite...")
        global ACTIVE_BACKEND
        if HAS_POSTGRES: 
            ACTIVE_BACKEND = "postgres"
            return "postgres"
        else:
            ACTIVE_BACKEND = "sqlite"
            return "sqlite"
    return None

def strip_line(line):
    # Data reduction optimization rule
    line = line.strip()
    if len(line) > 1000:
        return line[:1000] # Truncate massive lines (often HTML dumps)
    return line

# ==============================================================
# Admin Management
# ==============================================================

# Admins are read on EVERY incoming message (is_admin) but change rarely, so cache
# them in-process and refresh only when the set changes or a backend switch happens.
_admins_cache = None

def _invalidate_admins_cache():
    global _admins_cache
    _admins_cache = None

def get_admins(force_refresh=False):
    global _admins_cache
    if _admins_cache is not None and not force_refresh:
        return _admins_cache
    if ACTIVE_BACKEND == "mongo":
        admins = [doc['user_id'] for doc in admins_col.find()]
    elif ACTIVE_BACKEND == "postgres":
        conn = pg_pool.getconn()
        c = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        c.execute("SELECT user_id FROM admins")
        admins = [row['user_id'] for row in c.fetchall()]
        c.close(); pg_pool.putconn(conn)
    else:
        conn = _get_sqlite_conn()
        c = conn.cursor()
        c.execute("SELECT user_id FROM admins")
        admins = [row['user_id'] for row in c.fetchall()]
        conn.close()
    _admins_cache = admins
    return admins

def add_admin(user_id):
    _invalidate_admins_cache()
    if ACTIVE_BACKEND == "mongo":
        from pymongo.errors import DuplicateKeyError
        try:
            admins_col.insert_one({"user_id": user_id})
            return True
        except DuplicateKeyError:
            return False
    elif ACTIVE_BACKEND == "postgres":
        conn = pg_pool.getconn()
        try:
            c = conn.cursor()
            c.execute("INSERT INTO admins (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
            conn.commit()
            return True
        except: return False
        finally: c.close(); pg_pool.putconn(conn)
    else:
        conn = _get_sqlite_conn()
        try:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
            conn.commit()
            return True
        except: return False
        finally: conn.close()

def remove_admin(user_id):
    _invalidate_admins_cache()
    if ACTIVE_BACKEND == "mongo":
        admins_col.delete_one({"user_id": user_id})
        return True
    elif ACTIVE_BACKEND == "postgres":
        conn = pg_pool.getconn()
        c = conn.cursor()
        c.execute("DELETE FROM admins WHERE user_id = %s", (user_id,))
        conn.commit(); c.close(); pg_pool.putconn(conn)
        return True
    else:
        conn = _get_sqlite_conn()
        c = conn.cursor()
        c.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return True

# ==============================================================
# Saved ZIP/RAR passwords (tried automatically on encrypted archives)
# ==============================================================

_passwords_cache = None

def _invalidate_passwords_cache():
    global _passwords_cache
    _passwords_cache = None

def get_passwords(force_refresh=False):
    """Return the list of saved archive passwords (cached; read once per archive)."""
    global _passwords_cache
    if _passwords_cache is not None and not force_refresh:
        return _passwords_cache
    if ACTIVE_BACKEND == "mongo":
        pwds = [d['password'] for d in passwords_col.find({}, {"password": 1, "_id": 0})]
    elif ACTIVE_BACKEND == "postgres":
        conn = pg_pool.getconn()
        c = conn.cursor()
        c.execute("SELECT password FROM zip_passwords")
        pwds = [r[0] for r in c.fetchall()]
        c.close(); pg_pool.putconn(conn)
    else:
        conn = _get_sqlite_conn()
        c = conn.cursor()
        c.execute("SELECT password FROM zip_passwords")
        pwds = [r[0] for r in c.fetchall()]
        conn.close()
    _passwords_cache = pwds
    return pwds

def add_password(password):
    if not password:
        return False
    _invalidate_passwords_cache()
    if ACTIVE_BACKEND == "mongo":
        from pymongo.errors import DuplicateKeyError
        try:
            passwords_col.insert_one({"password": password}); return True
        except DuplicateKeyError:
            return False
    elif ACTIVE_BACKEND == "postgres":
        conn = pg_pool.getconn()
        try:
            c = conn.cursor()
            c.execute("INSERT INTO zip_passwords (password) VALUES (%s) ON CONFLICT DO NOTHING", (password,))
            conn.commit(); return c.rowcount > 0
        except: return False
        finally: c.close(); pg_pool.putconn(conn)
    else:
        conn = _get_sqlite_conn()
        try:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO zip_passwords (password) VALUES (?)", (password,))
            conn.commit(); return c.rowcount > 0
        except: return False
        finally: conn.close()

def remove_password(password):
    _invalidate_passwords_cache()
    if ACTIVE_BACKEND == "mongo":
        passwords_col.delete_one({"password": password}); return True
    elif ACTIVE_BACKEND == "postgres":
        conn = pg_pool.getconn()
        c = conn.cursor()
        c.execute("DELETE FROM zip_passwords WHERE password = %s", (password,))
        conn.commit(); c.close(); pg_pool.putconn(conn)
        return True
    else:
        conn = _get_sqlite_conn()
        c = conn.cursor()
        c.execute("DELETE FROM zip_passwords WHERE password = ?", (password,))
        conn.commit(); conn.close()
        return True

# ==============================================================
# Search Management
# ==============================================================

def create_search(user_id, search_terms, source):
    keywords = ", ".join(search_terms)
    
    # Fire and forget old cleanups randomly
    import random
    if random.random() < 0.1: clean_old_searches(14)
    
    if ACTIVE_BACKEND == "mongo":
        from pymongo.errors import OperationFailure
        try:
            result = searches_col.insert_one({
                "user_id": user_id,
                "keywords": keywords,
                "source": source,
                "timestamp": datetime.datetime.utcnow()
            })
            return str(result.inserted_id)
        except OperationFailure as e:
            if handle_mongo_quota_error(e):
                return create_search(user_id, search_terms, source)  # Retry with new backend
    
    elif ACTIVE_BACKEND == "postgres":
        conn = pg_pool.getconn()
        c = conn.cursor()
        c.execute("INSERT INTO searches (user_id, keywords, source) VALUES (%s, %s, %s) RETURNING id", 
                  (user_id, keywords, source))
        search_id = c.fetchone()[0]
        conn.commit(); c.close(); pg_pool.putconn(conn)
        return str(search_id)
    else:
        conn = _get_sqlite_conn()
        c = conn.cursor()
        c.execute("INSERT INTO searches (user_id, keywords, source) VALUES (?, ?, ?)", 
                  (user_id, keywords, source))
        search_id = c.lastrowid
        conn.commit()
        conn.close()
        return str(search_id)

def save_result(search_id, line, term):
    line = strip_line(line)
    h = _line_hash(term, line)
    if ACTIVE_BACKEND == "mongo":
        from pymongo.errors import DuplicateKeyError, OperationFailure
        try:
            results_col.insert_one({
                "search_id": str(search_id),
                "keyword": term,
                "result_line": line,
                "line_hash": h,
                "timestamp": datetime.datetime.utcnow()
            })
        except DuplicateKeyError: pass
        except OperationFailure as e:
            if handle_mongo_quota_error(e): save_result(search_id, line, term)

    elif ACTIVE_BACKEND == "postgres":
        conn = pg_pool.getconn()
        try:
            c = conn.cursor()
            c.execute("INSERT INTO results (search_id, keyword, result_line, line_hash) VALUES (%s, %s, %s, %s) ON CONFLICT (line_hash) DO NOTHING",
                      (str(search_id), term, line, h))
            conn.commit()
        except: pass
        finally: c.close(); pg_pool.putconn(conn)
    else:
        conn = _get_sqlite_conn()
        c = conn.cursor()
        try:
            c.execute("INSERT OR IGNORE INTO results (search_id, keyword, result_line, line_hash) VALUES (?, ?, ?, ?)",
                      (str(search_id), term, line, h))
            conn.commit()
        except: pass
        finally: conn.close()

def save_results_batch(batch):
    if not batch: return
    # Truncate long lines + precompute the dedup hash once per row.
    processed_batch = []
    for sid, line, kw in batch:
        sl = strip_line(line)
        processed_batch.append((str(sid), sl, kw, _line_hash(kw, sl)))

    if ACTIVE_BACKEND == "mongo":
        from pymongo.errors import BulkWriteError, OperationFailure
        docs = [{"search_id": sid, "keyword": kw, "result_line": line, "line_hash": h, "timestamp": datetime.datetime.utcnow()} for sid, line, kw, h in processed_batch]
        if docs:
            try:
                results_col.insert_many(docs, ordered=False)
            except BulkWriteError: pass  # dup-key rows skipped, rest inserted
            except OperationFailure as e:
                ns = handle_mongo_quota_error(e)
                if ns: save_results_batch(batch) # Re-try current batch on new backend
            except Exception: pass

    elif ACTIVE_BACKEND == "postgres":
        conn = pg_pool.getconn()
        try:
            c = conn.cursor()
            # execute_values sends all rows in ONE statement -> much faster than execute_batch.
            psycopg2.extras.execute_values(c,
                "INSERT INTO results (search_id, keyword, result_line, line_hash) VALUES %s ON CONFLICT (line_hash) DO NOTHING",
                [(sid, kw, line, h) for sid, line, kw, h in processed_batch],
                page_size=1000)
            conn.commit()
        except: pass
        finally: c.close(); pg_pool.putconn(conn)
    else:
        conn = _get_sqlite_conn()
        c = conn.cursor()
        try:
            c.executemany("INSERT OR IGNORE INTO results (search_id, keyword, result_line, line_hash) VALUES (?, ?, ?, ?)",
                          [(sid, kw, line, h) for sid, line, kw, h in processed_batch])
            conn.commit()
        except: pass
        finally: conn.close()

def export_full_database_csv(filepath):
    import csv
    if ACTIVE_BACKEND == "mongo":
        cursor = results_col.find({}, {"keyword": 1, "result_line": 1, "timestamp": 1, "_id": 0})
        total = 0
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['Keyword', 'Result Line', 'Timestamp'])
            for r in cursor:
                ts = r.get('timestamp')
                if isinstance(ts, datetime.datetime): ts = ts.strftime("%Y-%m-%d %H:%M:%S")
                w.writerow([r.get('keyword'), r.get('result_line'), ts])
                total += 1
        return total
    elif ACTIVE_BACKEND == "postgres":
        conn = pg_pool.getconn()
        c = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        c.execute("SELECT keyword, result_line, timestamp FROM results")
        total = 0
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['Keyword', 'Result Line', 'Timestamp'])
            while True:
                rows = c.fetchmany(10000)
                if not rows: break
                for r in rows:
                    w.writerow([r['keyword'], r['result_line'], str(r['timestamp'])])
                    total += 1
        c.close(); pg_pool.putconn(conn)
        return total
    else:
        conn = _get_sqlite_conn()
        c = conn.cursor()
        c.execute("SELECT keyword, result_line, timestamp FROM results")
        total = 0
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['Keyword', 'Result Line', 'Timestamp'])
            while True:
                rows = c.fetchmany(10000)
                if not rows: break
                for r in rows:
                    w.writerow([r['keyword'], r['result_line'], r['timestamp']])
                    total += 1
        conn.close()
        return total

def get_total_stats():
    if ACTIVE_BACKEND == "mongo":
        return {
            'total_searches': searches_col.count_documents({}),
            'total_keywords': len(results_col.distinct("keyword")),
            'total_unique_results': results_col.count_documents({}),
            'total_hits': results_col.count_documents({})
        }
    elif ACTIVE_BACKEND == "postgres":
        conn = pg_pool.getconn()
        c = conn.cursor()
        c.execute("SELECT COUNT(id) FROM searches")
        total_searches = c.fetchone()[0]
        c.execute("SELECT COUNT(DISTINCT keyword) FROM results")
        total_keywords = c.fetchone()[0]
        c.execute("SELECT COUNT(id) FROM results")
        total_results = c.fetchone()[0]
        c.close(); pg_pool.putconn(conn)
        return {
            'total_searches': total_searches,
            'total_keywords': total_keywords,
            'total_unique_results': total_results,
            'total_hits': total_results
        }
    else:
        conn = _get_sqlite_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(id) FROM searches")
        total_searches = c.fetchone()[0]
        c.execute("SELECT COUNT(DISTINCT keyword) FROM results")
        total_keywords = c.fetchone()[0]
        c.execute("SELECT COUNT(id) FROM results")
        total_unique_results = c.fetchone()[0]
        conn.close()
        return {
            'total_searches': total_searches,
            'total_keywords': total_keywords,
            'total_unique_results': total_unique_results,
            'total_hits': total_unique_results
        }

def get_keyword_stats(limit=15):
    if ACTIVE_BACKEND == "mongo":
        pipeline = [
            {"$group": {"_id": "$keyword", "total": {"$sum": 1}, "last_searched": {"$max": "$timestamp"}}},
            {"$sort": {"total": -1}}, {"$limit": limit}
        ]
        rows = list(results_col.aggregate(pipeline))
        return [{'keyword': r['_id'], 'total_hits': r['total'], 'unique_hits': r['total'],
                 'last_searched': r['last_searched'].strftime("%Y-%m-%d %H:%M:%S") if isinstance(r['last_searched'], datetime.datetime) else r['last_searched']
                } for r in rows]
    elif ACTIVE_BACKEND == "postgres":
        conn = pg_pool.getconn()
        c = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        c.execute("SELECT keyword, COUNT(id) as total, MAX(timestamp) as last_searched FROM results GROUP BY keyword ORDER BY total DESC LIMIT %s", (limit,))
        rows = c.fetchall()
        c.close(); pg_pool.putconn(conn)
        return [{'keyword': r['keyword'], 'total_hits': r['total'], 'unique_hits': r['total'], 'last_searched': str(r['last_searched'])} for r in rows]
    else:
        conn = _get_sqlite_conn()
        c = conn.cursor()
        c.execute("SELECT keyword, COUNT(id) as total, MAX(timestamp) as last_searched FROM results GROUP BY keyword ORDER BY total DESC LIMIT ?", (limit,))
        rows = c.fetchall()
        conn.close()
        return [{'keyword': r['keyword'], 'total_hits': r['total'], 'unique_hits': r['total'], 'last_searched': r['last_searched']} for r in rows]

def get_recent_searches(limit=10):
    if ACTIVE_BACKEND == "mongo":
        rows = list(searches_col.find().sort("timestamp", -1).limit(limit))
        return [{'user_id': r['user_id'], 'keywords': r['keywords'], 'source': r['source'],
                 'timestamp': r['timestamp'].strftime("%Y-%m-%d %H:%M:%S") if isinstance(r['timestamp'], datetime.datetime) else r['timestamp']
                } for r in rows]
    elif ACTIVE_BACKEND == "postgres":
        conn = pg_pool.getconn()
        c = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        c.execute("SELECT user_id, keywords, source, timestamp FROM searches ORDER BY id DESC LIMIT %s", (limit,))
        rows = c.fetchall()
        c.close(); pg_pool.putconn(conn)
        return [{'user_id': r['user_id'], 'keywords': r['keywords'], 'source': r['source'], 'timestamp': str(r['timestamp'])} for r in rows]
    else:
        conn = _get_sqlite_conn()
        c = conn.cursor()
        c.execute("SELECT user_id, keywords, source, timestamp FROM searches ORDER BY id DESC LIMIT ?", (limit,))
        rows = c.fetchall()
        conn.close()
        return [{'user_id': r['user_id'], 'keywords': r['keywords'], 'source': r['source'], 'timestamp': r['timestamp']} for r in rows]

def get_all_keywords_list():
    if ACTIVE_BACKEND == "mongo":
        return sorted(results_col.distinct("keyword"))
    elif ACTIVE_BACKEND == "postgres":
        conn = pg_pool.getconn()
        c = conn.cursor()
        c.execute("SELECT DISTINCT keyword FROM results ORDER BY keyword ASC")
        rows = [r[0] for r in c.fetchall()]
        c.close(); pg_pool.putconn(conn)
        return rows
    else:
        conn = _get_sqlite_conn()
        c = conn.cursor()
        c.execute("SELECT DISTINCT keyword FROM results ORDER BY keyword ASC")
        rows = c.fetchall()
        conn.close()
        return [r['keyword'] for r in rows]

def get_results_by_keyword(keyword, limit=50000, unique_only=True):
    if ACTIVE_BACKEND == "mongo":
        rows = list(results_col.find({"keyword": keyword}).sort("timestamp", -1).limit(limit))
        return [{'result_line': r['result_line'], 'timestamp': r['timestamp'].strftime("%Y-%m-%d %H:%M:%S") if isinstance(r['timestamp'], datetime.datetime) else r['timestamp']} for r in rows]
    elif ACTIVE_BACKEND == "postgres":
        conn = pg_pool.getconn()
        c = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        c.execute("SELECT result_line, timestamp FROM results WHERE keyword = %s ORDER BY id DESC LIMIT %s", (keyword, limit))
        rows = c.fetchall()
        c.close(); pg_pool.putconn(conn)
        return [{'result_line': r['result_line'], 'timestamp': str(r['timestamp'])} for r in rows]
    else:
        conn = _get_sqlite_conn()
        c = conn.cursor()
        c.execute("SELECT result_line, timestamp FROM results WHERE keyword = ? ORDER BY id DESC LIMIT ?", (keyword, limit))
        rows = c.fetchall()
        conn.close()
        return [{'result_line': r['result_line'], 'timestamp': r['timestamp']} for r in rows]

def import_results(keyword, results, user_id):
    imported = 0; skipped = 0; errors = 0

    # Preprocess + de-dup within this import, and precompute hashes.
    results = list(set([strip_line(line) for line in results]))
    rows = [(line, _line_hash(keyword, line)) for line in results]

    if ACTIVE_BACKEND == "mongo":
        from pymongo.errors import BulkWriteError
        search_result = searches_col.insert_one({"user_id": user_id, "keywords": keyword, "source": "CSV Import", "timestamp": datetime.datetime.utcnow()})
        search_id = str(search_result.inserted_id)
        docs = [{"search_id": search_id, "keyword": keyword, "result_line": line, "line_hash": h, "timestamp": datetime.datetime.utcnow()} for line, h in rows]
        if docs:
            try:
                results_col.insert_many(docs, ordered=False)
                imported = len(docs)
            except BulkWriteError as bwe:
                inserted = bwe.details['nInserted']; imported = inserted; skipped = len(docs) - inserted
            except Exception: errors = len(docs)
    elif ACTIVE_BACKEND == "postgres":
        conn = pg_pool.getconn()
        try:
            c = conn.cursor()
            c.execute("INSERT INTO searches (user_id, keywords, source) VALUES (%s, %s, %s) RETURNING id", (user_id, keyword, "CSV Import"))
            search_id = str(c.fetchone()[0])
            # One batched statement instead of a round-trip per row. RETURNING id comes
            # back only for rows that actually inserted, so its length = imported count.
            returned = psycopg2.extras.execute_values(c,
                "INSERT INTO results (search_id, keyword, result_line, line_hash) VALUES %s ON CONFLICT (line_hash) DO NOTHING RETURNING id",
                [(search_id, keyword, line, h) for line, h in rows], page_size=1000, fetch=True)
            imported = len(returned); skipped = len(rows) - imported
            conn.commit()
        except Exception: errors = len(rows)
        finally: c.close(); pg_pool.putconn(conn)
    else:
        conn = _get_sqlite_conn()
        try:
            c = conn.cursor()
            c.execute("INSERT INTO searches (user_id, keywords, source) VALUES (?, ?, ?)", (user_id, keyword, "CSV Import"))
            search_id = str(c.lastrowid)
            before = conn.total_changes
            c.executemany("INSERT OR IGNORE INTO results (search_id, keyword, result_line, line_hash) VALUES (?, ?, ?, ?)",
                          [(search_id, keyword, line, h) for line, h in rows])
            imported = conn.total_changes - before  # OR IGNORE skips don't count as changes
            skipped = len(rows) - imported
            conn.commit()
        except Exception: errors = len(rows)
        finally: conn.close()

    return {"imported": imported, "skipped": skipped, "errors": errors}
