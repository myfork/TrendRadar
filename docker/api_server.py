#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TrendRadar API Server - 独立的 JSON API 服务
提供新闻数据的 JSON 接口，供前端 app.html 使用

特性:
  - 缓存机制：启动时解析 index.html 生成 JSON 缓存
  - 自动更新：监听 index.html 文件变化，自动更新缓存
  - 静态文件：同时提供 output 目录的静态文件服务
  - 配置管理：支持在线编辑 config.yaml 和 frequency_words.txt

使用方法:
  python api_server.py [port]
  默认端口: 8081

API 端点:
  GET /api/topics   - 获取按主题聚合的新闻
  GET /api/sources  - 获取按来源聚合的新闻
  GET /api/status   - 获取系统状态
  GET /api/refresh  - 手动刷新缓存
  GET /api/config/frequency_words - 获取频率词配置
  POST /api/config/frequency_words - 保存频率词配置
  GET /api/config/platforms - 获取平台配置
  POST /api/config/platforms - 保存平台配置
"""

import json
import os
import re
import sys
import threading
import time
import yaml
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

# 导入后端的频率词加载函数
import sys as _sys
_sys.path.insert(0, '/app')
try:
    from trendradar.core.frequency import load_frequency_words
    FREQUENCY_WORDS_AVAILABLE = True
except ImportError as e:
    FREQUENCY_WORDS_AVAILABLE = False
    print(f"Warning: trendradar.core.frequency not available ({e}), topic matching disabled")

# 配置
API_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
OUTPUT_DIR = Path(__file__).parent.parent / "output"
STATIC_DIR = Path(__file__).parent  # docker 目录，存放 app.html 模板
CONFIG_DIR = Path("/app/config") if Path("/app/config").exists() else Path(__file__).parent.parent / "config"
FREQUENCY_WORDS_FILE = CONFIG_DIR / "frequency_words.txt"
CONFIG_YAML_FILE = CONFIG_DIR / "config.yaml"

# 全局缓存
cache = {
    "update_time": "",
    "topics": [],
    "sources": [],
    "last_modified": 0,
    "cache_time": ""
}
cache_lock = threading.Lock()

# 频率词缓存
frequency_cache = {
    "word_groups": [],
    "filter_words": [],
    "global_filters": [],
    "last_modified": 0
}


def load_frequency_config():
    """加载频率词配置"""
    if not FREQUENCY_WORDS_AVAILABLE:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] FREQUENCY_WORDS_AVAILABLE is False")
        return [], [], []
    
    freq_file = Path("/app/config/frequency_words.txt")
    if not freq_file.exists():
        freq_file = Path(__file__).parent.parent / "config" / "frequency_words.txt"
    
    if not freq_file.exists():
        print(f"[{datetime.now().strftime('%H:%M:%S')}] frequency_words.txt not found")
        return [], [], []
    
    try:
        current_mtime = freq_file.stat().st_mtime
        if current_mtime > frequency_cache["last_modified"]:
            word_groups, filter_words, global_filters = load_frequency_words(str(freq_file))
            frequency_cache["word_groups"] = word_groups
            frequency_cache["filter_words"] = filter_words
            frequency_cache["global_filters"] = global_filters
            frequency_cache["last_modified"] = current_mtime
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Loaded {len(word_groups)} word groups from frequency_words.txt")
        return frequency_cache["word_groups"], frequency_cache["filter_words"], frequency_cache["global_filters"]
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Error loading frequency words: {e}")
        import traceback
        traceback.print_exc()
        return [], [], []


def find_matched_topics(title: str, word_groups: list, filter_words: list, global_filters: list) -> list:
    """
    找出标题匹配的所有主题和关键词
    返回格式: [{"topic": "AI大模型", "matched": ["OpenAI", "ChatGPT"]}, ...]
    """
    if not title or not word_groups:
        return []
    
    title_lower = title.lower()
    
    # 全局过滤检查
    if global_filters:
        for gf in global_filters:
            if gf.lower() in title_lower:
                return []
    
    # 过滤词检查
    for fw in filter_words:
        if fw.lower() in title_lower:
            return []
    
    matched_topics = []
    
    for group in word_groups:
        required_words = group.get("required", [])
        normal_words = group.get("normal", [])
        group_key = group.get("group_key", "")
        
        # 必须词检查
        if required_words:
            all_required = all(rw.lower() in title_lower for rw in required_words)
            if not all_required:
                continue
        
        # 普通词检查 - 找出所有匹配的词
        matched_words = []
        if normal_words:
            for nw in normal_words:
                if nw.lower() in title_lower:
                    matched_words.append(nw)
            if not matched_words:
                continue
        
        # 添加必须词到匹配列表
        if required_words:
            matched_words = required_words + matched_words
        
        matched_topics.append({
            "topic": group_key,
            "matched": matched_words[:3]  # 最多显示3个匹配词
        })
    
    return matched_topics


def parse_index_html():
    """解析 index.html 提取新闻数据"""
    index_file = OUTPUT_DIR / "index.html"
    if not index_file.exists():
        return None, None, None
    
    html = index_file.read_text(encoding='utf-8')
    
    # 提取更新时间 - 匹配"生成时间"对应的值
    update_time = ""
    time_match = re.search(r'<span class="info-label">生成时间</span>\s*<span class="info-value">([^<]+)</span>', html)
    if time_match:
        update_time = time_match.group(1).strip()
    
    # 提取主题数据 - 使用更宽松的正则
    topics = []
    # 先找所有 word-group
    group_pattern = re.compile(r'<div class="word-group"[^>]*id="topic-(\d+)"[^>]*>(.*?)</div>\s*</div>\s*(?=<div class="word-group"|$)', re.DOTALL)
    name_pattern = re.compile(r'<div class="word-name">([^<]+)</div>')
    count_pattern = re.compile(r'<div class="word-count[^"]*">(\d+)\s*条</div>')
    # 修改正则以捕获 new 标记
    news_item_pattern = re.compile(r'<div class="news-item\s*(new)?[^"]*">(.*?)</div>\s*</div>\s*</div>', re.DOTALL)
    news_detail_pattern = re.compile(r'<span class="source-name">([^<]+)</span>.*?<a href="([^"]+)"[^>]*class="news-link">([^<]+)</a>', re.DOTALL)
    
    # 简化解析：直接按 word-group 分割（不依赖 id 属性）
    parts = re.split(r'<div class="word-group"[^>]*>', html)
    
    for i in range(1, len(parts)):
        content = parts[i]
        topic_id = i - 1
        
        # 截取到下一个 word-group 或文件末尾
        end_idx = content.find('<div class="word-group"')
        if end_idx > 0:
            content = content[:end_idx]
        
        name_match = name_pattern.search(content)
        count_match = count_pattern.search(content)
        
        topic_name = name_match.group(1).strip() if name_match else f"Topic {topic_id}"
        news_count = int(count_match.group(1)) if count_match else 0
        
        news_list = []
        # 使用更简单的方式解析新闻项
        news_items = re.findall(r'<div class="news-item\s*(new)?\s*">(.*?)</div>\s*</div>\s*</div>', content, re.DOTALL)
        for j, (is_new, item_content) in enumerate(news_items):
            detail_match = news_detail_pattern.search(item_content)
            if detail_match:
                title = detail_match.group(3).strip()
                news_list.append({
                    "rank": j + 1,
                    "source": detail_match.group(1).strip(),
                    "url": detail_match.group(2),
                    "title": title,
                    "isNew": bool(is_new),
                    "_title_for_match": title  # 临时保存用于后续匹配
                })
        
        topics.append({
            "id": topic_id,
            "name": topic_name,
            "count": news_count if news_count else len(news_list),
            "news": news_list
        })
    
    # 加载频率词配置
    word_groups, filter_words, global_filters = load_frequency_config()
    
    # 为 topics 中的新闻添加 matchedTopics，并构建关键词和分类映射
    topic_keywords = {}  # 主题名 -> 关键词列表
    topic_categories = {}  # 主题名 -> 分类
    for group in word_groups:
        topic_keywords[group["group_key"]] = group.get("normal", []) + group.get("required", [])
        topic_categories[group["group_key"]] = group.get("category", "其他")
    
    for topic in topics:
        topic_name = topic["name"]
        topic["keywords"] = topic_keywords.get(topic_name, [])
        topic["category"] = topic_categories.get(topic_name, "其他")
        for news in topic.get("news", []):
            title = news.get("_title_for_match", news["title"])
            matched = find_matched_topics(title, word_groups, filter_words, global_filters)
            news["matchedTopics"] = matched
            # 删除临时字段
            if "_title_for_match" in news:
                del news["_title_for_match"]
    
    # 按来源聚合
    sources = {}
    for topic in topics:
        for news in topic.get("news", []):
            source = news["source"]
            title = news["title"]
            if source not in sources:
                sources[source] = []
            
            # 计算匹配的主题和关键词
            matched_topics = find_matched_topics(title, word_groups, filter_words, global_filters)
            
            sources[source].append({
                "title": title,
                "url": news["url"],
                "topic": topic["name"],
                "matchedTopics": matched_topics,
                "isNew": news.get("isNew", False)
            })
    
    sources_list = [
        {"name": name, "count": len(news), "news": news}
        for name, news in sorted(sources.items(), key=lambda x: -len(x[1]))
    ]
    
    return update_time, topics, sources_list


def refresh_cache():
    """刷新缓存"""
    global cache
    index_file = OUTPUT_DIR / "index.html"
    
    try:
        update_time, topics, sources = parse_index_html()
        if topics is not None:
            with cache_lock:
                cache["update_time"] = update_time
                cache["topics"] = topics
                cache["sources"] = sources
                cache["last_modified"] = index_file.stat().st_mtime if index_file.exists() else 0
                cache["cache_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Cache refreshed: {len(topics)} topics, {len(sources)} sources")
            return True
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Cache refresh error: {e}")
    return False


def watch_file():
    """监听 index.html 文件变化"""
    index_file = OUTPUT_DIR / "index.html"
    last_mtime = cache["last_modified"]
    
    while True:
        try:
            if index_file.exists():
                current_mtime = index_file.stat().st_mtime
                if current_mtime > last_mtime:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] index.html changed, refreshing cache...")
                    refresh_cache()
                    last_mtime = current_mtime
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Watch error: {e}")
        time.sleep(5)  # 每5秒检查一次


def copy_app_html():
    """复制 app.html 到 output 目录"""
    src = STATIC_DIR / "app.html"
    dst = OUTPUT_DIR / "app.html"
    if src.exists() and (not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime):
        dst.write_text(src.read_text(encoding='utf-8'), encoding='utf-8')
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Copied app.html to output/")


class APIHandler(SimpleHTTPRequestHandler):
    """API 请求处理器"""
    
    # 爬虫状态
    crawl_status = {"running": False, "last_run": "", "message": ""}
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(OUTPUT_DIR), **kwargs)
    
    def trigger_crawl(self):
        """触发爬虫任务"""
        import subprocess
        import sys
        
        if APIHandler.crawl_status["running"]:
            return {"success": False, "message": "爬虫正在运行中，请稍后再试"}
        
        def run_crawl():
            APIHandler.crawl_status["running"] = True
            APIHandler.crawl_status["message"] = "爬虫运行中..."
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始手动爬取...", flush=True)
            try:
                result = subprocess.run(
                    ["/usr/local/bin/python", "-m", "trendradar"],
                    cwd="/app",
                    timeout=300,  # 5分钟超时
                    stdout=sys.stdout,
                    stderr=sys.stderr
                )
                if result.returncode == 0:
                    APIHandler.crawl_status["message"] = "爬虫完成"
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 手动爬取完成", flush=True)
                else:
                    APIHandler.crawl_status["message"] = f"爬虫失败 (返回码: {result.returncode})"
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 手动爬取失败", flush=True)
            except subprocess.TimeoutExpired:
                APIHandler.crawl_status["message"] = "爬虫超时"
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 手动爬取超时", flush=True)
            except Exception as e:
                APIHandler.crawl_status["message"] = f"爬虫错误: {str(e)}"
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 手动爬取错误: {e}", flush=True)
            finally:
                APIHandler.crawl_status["running"] = False
                APIHandler.crawl_status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 在后台线程运行爬虫
        threading.Thread(target=run_crawl, daemon=True).start()
        return {"success": True, "message": "爬虫任务已启动"}
    
    def get_all_news(self):
        """从数据库获取全部新闻（不过滤关键词），按配置的来源顺序排列"""
        import sqlite3
        
        # 获取今天的数据库路径
        today = datetime.now().strftime("%Y-%m-%d")
        db_path = OUTPUT_DIR / today / "news.db"
        
        if not db_path.exists():
            return {"success": False, "error": "今日数据库不存在", "sources": []}
        
        try:
            # 读取平台配置顺序
            platform_order = {}
            if CONFIG_YAML_FILE.exists():
                with open(CONFIG_YAML_FILE, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f)
                platforms_config = config.get('platforms', [])
                for i, p in enumerate(platforms_config):
                    platform_order[p['id']] = {'order': i, 'name': p['name']}
            
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 查询所有新闻，按平台和排名分组
            cursor.execute("""
                SELECT n.title, n.url, n.rank, n.platform_id, p.name as platform_name,
                       n.first_crawl_time, n.last_crawl_time
                FROM news_items n
                LEFT JOIN platforms p ON n.platform_id = p.id
                ORDER BY n.platform_id, n.rank
            """)
            
            rows = cursor.fetchall()
            conn.close()
            
            # 按来源分组
            sources_dict = {}
            for row in rows:
                platform_id = row['platform_id']
                platform_name = row['platform_name'] or platform_id
                
                # 使用配置中的名称（如果有）
                if platform_id in platform_order:
                    platform_name = platform_order[platform_id]['name']
                
                if platform_name not in sources_dict:
                    sources_dict[platform_name] = {
                        'id': platform_id,
                        'name': platform_name,
                        'order': platform_order.get(platform_id, {}).get('order', 999),
                        'news': []
                    }
                
                sources_dict[platform_name]['news'].append({
                    'title': row['title'],
                    'url': row['url'],
                    'rank': row['rank']
                })
            
            # 按配置顺序排序
            sources_list = sorted(sources_dict.values(), key=lambda x: x['order'])
            
            # 添加 count 并移除 order 字段
            for s in sources_list:
                s['count'] = len(s['news'])
                del s['order']
            
            return {
                "success": True,
                "update_time": cache.get("update_time", ""),
                "sources": sources_list,
                "total": sum(s['count'] for s in sources_list)
            }
            
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] get_all_news error: {e}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e), "sources": []}
    
    def search_news(self, keyword):
        """搜索新闻"""
        import sqlite3
        
        if not keyword or not keyword.strip():
            return {"success": False, "error": "请输入搜索关键词", "results": []}
        
        keyword = keyword.strip()
        
        # 获取今天的数据库路径
        today = datetime.now().strftime("%Y-%m-%d")
        db_path = OUTPUT_DIR / today / "news.db"
        
        if not db_path.exists():
            return {"success": False, "error": "今日数据库不存在", "results": []}
        
        try:
            # 读取平台配置
            platform_names = {}
            if CONFIG_YAML_FILE.exists():
                with open(CONFIG_YAML_FILE, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f)
                for p in config.get('platforms', []):
                    platform_names[p['id']] = p['name']
            
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 搜索标题包含关键词的新闻
            cursor.execute("""
                SELECT n.title, n.url, n.rank, n.platform_id, p.name as platform_name
                FROM news_items n
                LEFT JOIN platforms p ON n.platform_id = p.id
                WHERE n.title LIKE ?
                ORDER BY n.rank
                LIMIT 200
            """, (f'%{keyword}%',))
            
            rows = cursor.fetchall()
            conn.close()
            
            results = []
            for row in rows:
                platform_id = row['platform_id']
                platform_name = platform_names.get(platform_id, row['platform_name'] or platform_id)
                results.append({
                    'title': row['title'],
                    'url': row['url'],
                    'rank': row['rank'],
                    'source': platform_name
                })
            
            return {
                "success": True,
                "keyword": keyword,
                "count": len(results),
                "results": results
            }
            
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] search_news error: {e}")
            return {"success": False, "error": str(e), "results": []}
    
    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
    
    def do_GET(self):
        path = urlparse(self.path).path
        
        if path == '/api/topics':
            with cache_lock:
                self.send_json({"update_time": cache["update_time"], "topics": cache["topics"], "cache_time": cache["cache_time"]})
        
        elif path == '/api/sources':
            with cache_lock:
                self.send_json({"update_time": cache["update_time"], "sources": cache["sources"], "cache_time": cache["cache_time"]})
        
        elif path == '/api/status':
            with cache_lock:
                self.send_json({
                    "status": "ok",
                    "update_time": cache["update_time"],
                    "topics_count": len(cache["topics"]),
                    "sources_count": len(cache["sources"]),
                    "cache_time": cache["cache_time"],
                    "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
        
        elif path == '/api/refresh':
            success = refresh_cache()
            self.send_json({"success": success, "cache_time": cache["cache_time"]})
        
        elif path == '/api/crawl':
            # 触发爬虫任务
            self.send_json(self.trigger_crawl())
        
        elif path == '/api/crawl_status':
            # 查询爬虫状态
            self.send_json({
                "running": APIHandler.crawl_status["running"],
                "last_run": APIHandler.crawl_status["last_run"],
                "message": APIHandler.crawl_status["message"]
            })
        
        elif path == '/api/config/frequency_words':
            # 获取频率词配置
            try:
                if FREQUENCY_WORDS_FILE.exists():
                    content = FREQUENCY_WORDS_FILE.read_text(encoding='utf-8')
                    self.send_json({"success": True, "content": content})
                else:
                    self.send_json({"success": False, "error": "文件不存在"}, 404)
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, 500)
        
        elif path == '/api/config/platforms':
            # 获取平台配置
            try:
                if CONFIG_YAML_FILE.exists():
                    with open(CONFIG_YAML_FILE, 'r', encoding='utf-8') as f:
                        config = yaml.safe_load(f)
                    platforms = config.get('platforms', [])
                    self.send_json({"success": True, "platforms": platforms})
                else:
                    self.send_json({"success": False, "error": "配置文件不存在"}, 404)
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, 500)
        
        elif path == '/api/allnews':
            # 获取全部新闻（从数据库读取，不过滤关键词）
            self.send_json(self.get_all_news())
        
        elif path.startswith('/api/search'):
            # 搜索新闻
            from urllib.parse import parse_qs
            query_string = urlparse(self.path).query
            params = parse_qs(query_string)
            keyword = params.get('q', [''])[0]
            self.send_json(self.search_news(keyword))
        
        elif path.startswith('/api/'):
            self.send_json({"error": "Unknown API endpoint"}, 404)
        
        else:
            super().do_GET()
    
    def do_POST(self):
        """处理 POST 请求"""
        path = urlparse(self.path).path
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length).decode('utf-8')
        
        if path == '/api/config/frequency_words':
            # 保存频率词配置
            try:
                data = json.loads(post_data)
                content = data.get('content', '')
                # 备份原文件
                if FREQUENCY_WORDS_FILE.exists():
                    backup_file = FREQUENCY_WORDS_FILE.with_suffix('.txt.bak')
                    backup_file.write_text(FREQUENCY_WORDS_FILE.read_text(encoding='utf-8'), encoding='utf-8')
                # 写入新内容
                FREQUENCY_WORDS_FILE.write_text(content, encoding='utf-8')
                # 清除频率词缓存，强制重新加载
                frequency_cache["last_modified"] = 0
                # 刷新数据缓存
                refresh_cache()
                self.send_json({"success": True, "message": "保存成功"})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, 500)
        
        elif path == '/api/config/platforms':
            # 保存平台配置
            try:
                data = json.loads(post_data)
                platforms = data.get('platforms', [])
                
                if CONFIG_YAML_FILE.exists():
                    # 读取现有配置
                    with open(CONFIG_YAML_FILE, 'r', encoding='utf-8') as f:
                        config = yaml.safe_load(f) or {}
                    # 备份
                    backup_file = CONFIG_YAML_FILE.with_suffix('.yaml.bak')
                    with open(backup_file, 'w', encoding='utf-8') as f:
                        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
                    # 更新 platforms
                    config['platforms'] = platforms
                    # 写入
                    with open(CONFIG_YAML_FILE, 'w', encoding='utf-8') as f:
                        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
                    self.send_json({"success": True, "message": "保存成功"})
                else:
                    self.send_json({"success": False, "error": "配置文件不存在"}, 404)
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, 500)
        
        else:
            self.send_json({"error": "Unknown API endpoint"}, 404)
    
    def do_OPTIONS(self):
        """处理 CORS 预检请求"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def log_message(self, format, *args):
        if '/api/' in args[0]:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")


def main():
    print(f"TrendRadar API Server v1.0")
    print(f"=" * 40)
    print(f"  Port: {API_PORT}")
    print(f"  Output: {OUTPUT_DIR}")
    print(f"  API Endpoints:")
    print(f"    GET /api/topics  - Topics data")
    print(f"    GET /api/sources - Sources data") 
    print(f"    GET /api/status  - Server status")
    print(f"    GET /api/refresh - Refresh cache")
    print(f"  App: http://localhost:{API_PORT}/app.html")
    print(f"=" * 40)
    
    # 复制 app.html
    copy_app_html()
    
    # 初始化缓存
    print("Initializing cache...")
    refresh_cache()
    
    # 启动文件监听线程
    watcher = threading.Thread(target=watch_file, daemon=True)
    watcher.start()
    print("File watcher started.")
    
    # 启动 HTTP 服务器
    server = HTTPServer(('0.0.0.0', API_PORT), APIHandler)
    print(f"Server running at http://0.0.0.0:{API_PORT}/")
    print()
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
