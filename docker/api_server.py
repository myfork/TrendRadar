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

# 导入频率词扩展模块（支持 #主题 @分类 语法 + 上游正则语法）
try:
    from frequency_helper import load_frequency_words_extended, find_matched_topics
    FREQUENCY_WORDS_AVAILABLE = True
except ImportError as e:
    FREQUENCY_WORDS_AVAILABLE = False
    print(f"Warning: frequency_helper not available ({e}), topic matching disabled")

# 配置
API_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
OUTPUT_DIR = Path(__file__).parent.parent / "output"
STATIC_DIR = Path(__file__).parent  # docker 目录，存放 app.html 模板
CONFIG_DIR = Path("/app/config") if Path("/app/config").exists() else Path(__file__).parent.parent / "config"
FREQUENCY_WORDS_FILE = CONFIG_DIR / "frequency_words.txt"
CONFIG_YAML_FILE = CONFIG_DIR / "config.yaml"
USER_SETTINGS_FILE = CONFIG_DIR / "user_settings.json"

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
            word_groups, filter_words, global_filters = load_frequency_words_extended(str(freq_file))
            frequency_cache["word_groups"] = word_groups
            frequency_cache["filter_words"] = filter_words
            frequency_cache["global_filters"] = global_filters
            frequency_cache["last_modified"] = current_mtime
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Loaded {len(frequency_cache['word_groups'])} word groups from frequency_words.txt")
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Error loading frequency words: {e}")
        import traceback
        traceback.print_exc()
        return [], [], []
    
    return frequency_cache["word_groups"], frequency_cache["filter_words"], frequency_cache["global_filters"]


def get_user_settings():
    """获取用户自定义设置"""
    try:
        if USER_SETTINGS_FILE.exists():
            return json.loads(USER_SETTINGS_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Error loading user settings: {e}")
    return {}


def save_user_settings(settings):
    """保存用户自定义设置"""
    try:
        current_settings = get_user_settings()
        current_settings.update(settings)
        USER_SETTINGS_FILE.write_text(json.dumps(current_settings, ensure_ascii=False, indent=2), encoding='utf-8')
        return True
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Error saving user settings: {e}")
        return False


def normalize_platforms_for_save(posted_platforms):
    posted_platforms = posted_platforms or []

    existing_platforms = (get_user_settings() or {}).get('platforms', []) or []
    existing_by_id = {p.get('id'): p for p in existing_platforms if isinstance(p, dict) and p.get('id')}

    config = {}
    try:
        if CONFIG_YAML_FILE.exists():
            with open(CONFIG_YAML_FILE, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
    except Exception:
        config = {}

    config_platforms = config.get('platforms', []) or []
    config_by_id = {p.get('id'): p for p in config_platforms if isinstance(p, dict) and p.get('id')}

    normalized = []
    seen = set()

    for p in posted_platforms:
        if not isinstance(p, dict):
            continue
        pid = p.get('id')
        if not pid or pid in seen:
            continue
        base = existing_by_id.get(pid, {}) or {}
        enabled = p.get('enabled', base.get('enabled', True))
        name = (config_by_id.get(pid, {}) or {}).get('name') or p.get('name') or base.get('name') or pid
        normalized.append({"id": pid, "name": name, "enabled": bool(enabled)})
        seen.add(pid)

    for pid, cp in config_by_id.items():
        if pid in seen:
            continue
        base = existing_by_id.get(pid, {}) or {}
        enabled = base.get('enabled', True)
        name = cp.get('name') or base.get('name') or pid
        normalized.append({"id": pid, "name": name, "enabled": bool(enabled)})
        seen.add(pid)

    for pid, ep in existing_by_id.items():
        if pid in seen:
            continue
        normalized.append({
            "id": pid,
            "name": ep.get('name', pid),
            "enabled": bool(ep.get('enabled', True))
        })
        seen.add(pid)

    return normalized


def get_combined_config():
    """获取合并后的配置（config.yaml + user_settings.json）"""
    config = {}
    try:
        if CONFIG_YAML_FILE.exists():
            with open(CONFIG_YAML_FILE, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Error loading config.yaml: {e}")
    
    user_settings = get_user_settings()
    
    # 合并配置 - 仅覆盖特定字段
    if 'platforms' in user_settings:
        # 智能合并 platforms: 使用 user_settings 的顺序和状态，但保留 config 中存在但 user_settings 中缺失的项
        user_platforms = user_settings['platforms']
        user_ids = [p['id'] for p in user_platforms]
        config_platforms = config.get('platforms', [])
        
        merged_platforms = []
        # 1. 添加用户配置的平台（保持用户顺序）
        for up in user_platforms:
            # 查找原始配置以获取完整信息（如果需要），这里主要保留 enabled 状态
            # 如果 user_settings 中有，说明用户已保存过，直接使用
            if 'enabled' not in up:
                 up['enabled'] = True
            merged_platforms.append(up)
            
        # 2. 添加 config 中有但 user_settings 中没有的平台（追加到末尾，默认启用）
        for cp in config_platforms:
            if cp['id'] not in user_ids:
                # 确保有 enabled 字段，默认为 True
                if 'enabled' not in cp:
                    cp['enabled'] = True
                merged_platforms.append(cp)
                
        # 去重并统一名称（以 config.yaml 为准）
        config_platforms_map = {cp['id']: cp.get('name', cp['id']) for cp in config_platforms}
        seen_ids = set()
        unified_platforms = []
        for p in merged_platforms:
            pid = p.get('id')
            if not pid or pid in seen_ids:
                continue
            p['name'] = config_platforms_map.get(pid, p.get('name', pid))
            unified_platforms.append(p)
            seen_ids.add(pid)
        # 二次补齐：确保所有 config.yaml 中的平台都在最终列表
        for cp in config_platforms:
            if cp['id'] not in seen_ids:
                unified_platforms.append({
                    "id": cp['id'],
                    "name": cp.get('name', cp['id']),
                    "enabled": True
                })
                seen_ids.add(cp['id'])
        config['platforms'] = unified_platforms
        # 自愈修复：如果 user_settings 中缺少平台项，则同步修复写回
        try:
            if len(user_platforms) != len(unified_platforms):
                current_settings = get_user_settings()
                current_settings['platforms'] = unified_platforms
                USER_SETTINGS_FILE.write_text(json.dumps(current_settings, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass
    else:
        config_platforms = config.get('platforms', []) or []
        seeded_platforms = []
        for cp in config_platforms:
            if not isinstance(cp, dict):
                continue
            pid = cp.get('id')
            if not pid:
                continue
            seeded_platforms.append({
                "id": pid,
                "name": cp.get('name', pid),
                "enabled": bool(cp.get('enabled', True))
            })
        if seeded_platforms:
            config['platforms'] = seeded_platforms
            try:
                current_settings = get_user_settings()
                if not current_settings.get('platforms'):
                    current_settings['platforms'] = seeded_platforms
                    USER_SETTINGS_FILE.write_text(json.dumps(current_settings, ensure_ascii=False, indent=2), encoding='utf-8')
            except Exception:
                pass

    if 'ui_tabs' in user_settings:
        config['ui_tabs'] = user_settings['ui_tabs']
    if 'topics_order' in user_settings:
        config['topics_order'] = user_settings['topics_order']
        
    return config


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
    
    # 构建关键词和分类映射（按索引顺序，因为 HTML 中的主题名可能是关键词拼接）
    # word_groups 的顺序和 HTML 中的主题顺序一致
    for i, topic in enumerate(topics):
        if i < len(word_groups):
            group = word_groups[i]
            # 用 frequency_helper 的 group_key 覆盖 HTML 解析的主题名
            topic["name"] = group["group_key"]
            topic["keywords"] = group.get("keywords", [])
            topic["category"] = group.get("category", "其他")
        else:
            topic["keywords"] = []
            topic["category"] = "其他"
        
        # 为新闻添加 matchedTopics
        for news in topic.get("news", []):
            title = news.get("_title_for_match", news["title"])
            matched = find_matched_topics(title, word_groups, filter_words, global_filters)
            news["matchedTopics"] = matched
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
    
    # 读取平台配置顺序
    platform_order = {}
    config = get_combined_config()
    platforms_config = config.get('platforms', [])
    for i, p in enumerate(platforms_config):
        platform_order[p['name']] = i
    enabled_by_name = {p['name']: p.get('enabled', True) for p in platforms_config}
    
    # 按配置顺序排序（未配置的排在最后，按新闻数量降序）
    sources_list = [
        {"name": name, "count": len(news), "news": news, "order": platform_order.get(name, 999)}
        for name, news in sources.items()
    ]
    # 过滤掉被禁用的平台
    sources_list = [s for s in sources_list if enabled_by_name.get(s['name'], True)]
    sources_list.sort(key=lambda x: (x['order'], -x['count']))
    # 移除 order 字段
    for s in sources_list:
        del s['order']
    
    return update_time, topics, sources_list


def get_data_from_db():
    """
    从数据库获取新闻数据，按主题和来源分组
    不依赖 index.html，直接使用 frequency_helper 进行关键词匹配
    """
    import sqlite3
    
    # 获取今天的数据库路径
    today = datetime.now().strftime("%Y-%m-%d")
    db_path = OUTPUT_DIR / "news" / f"{today}.db"
    
    if not db_path.exists():
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Database not found: {db_path}")
        return None, None, None
    
    # 加载频率词配置
    word_groups, filter_words, global_filters = load_frequency_config()
    if not word_groups:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] No word groups loaded")
        return None, None, None
    
    # 读取平台配置
    config = get_combined_config()
    platforms_config = config.get('platforms', [])
    platform_order = {}
    platform_names = {}
    platform_enabled = {}
    for i, p in enumerate(platforms_config):
        platform_order[p['id']] = i
        platform_names[p['id']] = p['name']
        platform_enabled[p['id']] = p.get('enabled', True)
    
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # 查询所有新闻
        cursor.execute("""
            SELECT n.title, n.url, n.rank, n.platform_id, p.name as platform_name,
                   n.first_crawl_time, n.last_crawl_time
            FROM news_items n
            LEFT JOIN platforms p ON n.platform_id = p.id
            ORDER BY n.platform_id, n.rank
        """)
        rows = cursor.fetchall()
        
        # 获取最新爬取时间作为更新时间
        cursor.execute("SELECT MAX(last_crawl_time) as latest FROM news_items")
        latest_row = cursor.fetchone()
        update_time = latest_row['latest'] if latest_row and latest_row['latest'] else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        conn.close()
        
        # 判断新闻是否为"新增"（first_crawl_time 在最近1小时内）
        from datetime import timedelta
        now = datetime.now()
        one_hour_ago = now - timedelta(hours=1)
        
        # 初始化主题数据结构
        topics = []
        for i, group in enumerate(word_groups):
            topics.append({
                "id": i,
                "name": group["group_key"],
                "keywords": group.get("keywords", []),
                "category": group.get("category", "其他"),
                "filter": group.get("filter", []),
                "count": 0,
                "news": []
            })
        
        # 按来源分组的数据
        sources_dict = {}
        
        # 用于去重（同一新闻可能匹配多个主题）
        topic_news_seen = {i: set() for i in range(len(topics))}
        
        # 处理每条新闻
        for row in rows:
            platform_id = row['platform_id']
            platform_name = platform_names.get(platform_id, row['platform_name'] or platform_id)
            
            # 跳过禁用的平台
            if not platform_enabled.get(platform_id, True):
                continue
            
            title = row['title']
            url = row['url']
            rank = row['rank']
            
            # 判断是否为新增新闻
            is_new = False
            if row['first_crawl_time']:
                try:
                    first_time = datetime.strptime(row['first_crawl_time'], "%Y-%m-%d %H:%M:%S")
                    is_new = first_time > one_hour_ago
                except:
                    pass
            
            # 使用 frequency_helper 匹配主题
            matched_topics = find_matched_topics(title, word_groups, filter_words, global_filters)
            
            news_item = {
                "title": title,
                "url": url,
                "rank": rank,
                "source": platform_name,
                "isNew": is_new,
                "matchedTopics": matched_topics
            }
            
            # 添加到匹配的主题中
            for mt in matched_topics:
                topic_name = mt["topic"]
                # 找到对应的主题索引
                for i, topic in enumerate(topics):
                    if topic["name"] == topic_name:
                        # 去重：同一标题不重复添加到同一主题
                        if title not in topic_news_seen[i]:
                            topic_news_seen[i].add(title)
                            topics[i]["news"].append(news_item.copy())
                        break
            
            # 添加到来源分组（只要新闻匹配了任意主题就添加）
            if matched_topics:
                if platform_name not in sources_dict:
                    sources_dict[platform_name] = {
                        "id": platform_id,
                        "name": platform_name,
                        "order": platform_order.get(platform_id, 999),
                        "news": []
                    }
                sources_dict[platform_name]["news"].append(news_item)
        
        # 更新主题的 count 并按热度排序新闻
        for topic in topics:
            # 按 rank 排序
            topic["news"].sort(key=lambda x: x.get("rank", 999))
            topic["count"] = len(topic["news"])
        
        # 按配置顺序排序来源
        sources_list = sorted(sources_dict.values(), key=lambda x: x['order'])
        for s in sources_list:
            s['count'] = len(s['news'])
            del s['order']
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] DB data loaded: {len(topics)} topics, {len(sources_list)} sources, {sum(t['count'] for t in topics)} matched news")
        return update_time, topics, sources_list
        
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] get_data_from_db error: {e}")
        import traceback
        traceback.print_exc()
        return None, None, None


def refresh_cache():
    """刷新缓存 - 优先使用数据库，失败时回退到 HTML 解析"""
    global cache
    
    try:
        # 优先从数据库获取数据
        update_time, topics, sources = get_data_from_db()
        
        # 如果数据库方式失败，回退到 HTML 解析
        if topics is None:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] DB method failed, falling back to HTML parsing...")
            update_time, topics, sources = parse_index_html()
        
        if topics is not None:
            # 应用主题顺序配置
            topics = apply_topics_order(topics)
            with cache_lock:
                cache["update_time"] = update_time
                cache["topics"] = topics
                cache["sources"] = sources
                cache["last_modified"] = time.time()
                cache["cache_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Cache refreshed: {len(topics)} topics, {len(sources)} sources")
            return True
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Cache refresh error: {e}")
        import traceback
        traceback.print_exc()
    return False


def apply_topics_order(topics):
    """应用保存的主题顺序配置"""
    try:
        config = get_combined_config()
        topics_order = config.get('topics_order', None)
        if topics_order:
            # 使用名称匹配（更可靠，避免 id 类型不匹配问题）
            topics_map = {t['name']: t for t in topics}
            ordered_topics = []
            saved_names = []
            
            # 按保存顺序添加启用的主题
            for saved in topics_order:
                topic_name = saved.get('name')
                if topic_name in topics_map:
                    # 无论是否启用，都从 topics_map 中获取最新数据，并保留 saved 中的 enabled 状态
                    topic_data = topics_map[topic_name]
                    # 如果 saved 中有 enabled 字段，则使用它，否则默认为 True
                    # 注意：前端可能需要 enabled 字段来决定是否渲染，但后端应该始终返回所有主题，或者根据参数决定
                    # 这里我们遵循原逻辑：后端返回列表供前端渲染，前端根据 enabled 字段决定是否显示内容或仅显示开关
                    # 但是，如果这里过滤掉了 enabled=False 的主题，前端就彻底拿不到数据了，导致无法重新开启
                    
                    # 修正：始终返回所有在 topics_order 中的主题，并附带 enabled 状态
                    topic_data['enabled'] = saved.get('enabled', True)
                    ordered_topics.append(topic_data)
                    saved_names.append(topic_name)
            
            # 添加新主题（不在保存顺序中的），默认启用
            for topic in topics:
                if topic['name'] not in saved_names:
                    topic['enabled'] = True
                    ordered_topics.append(topic)
            
            return ordered_topics
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] apply_topics_order error: {e}")
    return topics


def watch_file():
    """监听数据库文件和配置文件变化"""
    last_db_mtime = 0
    last_freq_mtime = 0
    
    while True:
        try:
            # 监听今天的数据库文件
            today = datetime.now().strftime("%Y-%m-%d")
            db_path = OUTPUT_DIR / "news" / f"{today}.db"
            
            # 监听 frequency_words.txt
            freq_file = FREQUENCY_WORDS_FILE
            
            need_refresh = False
            
            if db_path.exists():
                current_db_mtime = db_path.stat().st_mtime
                if current_db_mtime > last_db_mtime:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Database changed, refreshing cache...")
                    last_db_mtime = current_db_mtime
                    need_refresh = True
            
            if freq_file.exists():
                current_freq_mtime = freq_file.stat().st_mtime
                if current_freq_mtime > last_freq_mtime:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] frequency_words.txt changed, refreshing cache...")
                    last_freq_mtime = current_freq_mtime
                    # 重置频率词缓存
                    frequency_cache["last_modified"] = 0
                    need_refresh = True
            
            if need_refresh:
                refresh_cache()
                
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

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()
    
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
        
        # 获取今天的数据库路径（新格式：output/news/{date}.db）
        today = datetime.now().strftime("%Y-%m-%d")
        db_path = OUTPUT_DIR / "news" / f"{today}.db"
        
        if not db_path.exists():
            return {"success": False, "error": "今日数据库不存在", "sources": []}
        
        try:
            # 读取平台配置顺序
            platform_order = {}
            config = get_combined_config()
            platforms_config = config.get('platforms', [])
            for i, p in enumerate(platforms_config):
                platform_order[p['id']] = {'order': i, 'name': p['name']}
            platform_enabled_id = {p['id']: p.get('enabled', True) for p in platforms_config}
            platform_enabled_name = {p['name']: p.get('enabled', True) for p in platforms_config}
            
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
            
            # 按配置顺序排序并过滤禁用平台
            sources_list = [
                s for s in sorted(sources_dict.values(), key=lambda x: x['order'])
                if platform_enabled_id.get(s['id'], platform_enabled_name.get(s['name'], True))
            ]
            
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
        
        # 获取今天的数据库路径（新格式：output/news/{date}.db）
        today = datetime.now().strftime("%Y-%m-%d")
        db_path = OUTPUT_DIR / "news" / f"{today}.db"
        
        if not db_path.exists():
            return {"success": False, "error": "今日数据库不存在", "results": []}
        
        try:
            # 读取平台配置
            platform_names = {}
            config = get_combined_config()
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
                config = get_combined_config()
                if config:
                    platforms = config.get('platforms', [])
                    self.send_json({"success": True, "platforms": platforms})
                else:
                    self.send_json({"success": False, "error": "配置文件不存在"}, 404)
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, 500)
        
        elif path == '/api/config/tabs':
            # 获取模块顺序配置
            try:
                config = get_combined_config()
                if config:
                    tabs = config.get('ui_tabs', None)
                    self.send_json({"success": True, "tabs": tabs})
                else:
                    self.send_json({"success": True, "tabs": None})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, 500)
        
        elif path == '/api/config/topics_order':
            # 获取主题顺序配置
            try:
                config = get_combined_config()
                if config:
                    topics_order = config.get('topics_order', None)
                    self.send_json({"success": True, "topics_order": topics_order})
                else:
                    self.send_json({"success": True, "topics_order": None})
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
                platforms = normalize_platforms_for_save(data.get('platforms', []))
                
                if save_user_settings({'platforms': platforms}):
                    # 刷新缓存以应用新的来源顺序
                    # 注意：前端可能只发回了选中的平台（取决于前端逻辑）
                    # 但我们的 get_combined_config 会把缺失的补回来，所以这里保存是安全的
                    refresh_cache()
                    self.send_json({"success": True, "message": "保存成功"})
                else:
                    self.send_json({"success": False, "error": "保存失败"}, 500)
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, 500)
        
        elif path == '/api/config/tabs':
            # 保存模块顺序配置
            try:
                data = json.loads(post_data)
                tabs = data.get('tabs', [])
                
                if save_user_settings({'ui_tabs': tabs}):
                    self.send_json({"success": True, "message": "保存成功"})
                else:
                    self.send_json({"success": False, "error": "保存失败"}, 500)
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, 500)
        
        elif path == '/api/config/topics_order':
            # 保存主题顺序配置
            try:
                data = json.loads(post_data)
                topics_order = data.get('topics_order', [])
                
                if save_user_settings({'topics_order': topics_order}):
                    # 刷新缓存以应用新的主题顺序
                    refresh_cache()
                    self.send_json({"success": True, "message": "保存成功"})
                else:
                    self.send_json({"success": False, "error": "保存失败"}, 500)
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
        if args and isinstance(args[0], str) and '/api/' in args[0]:
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
