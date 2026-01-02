"""
数据解析服务

v2.0.0: 仅支持 SQLite 数据库，移除 TXT 文件支持
新存储结构：output/{type}/{date}.db
"""

import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from datetime import datetime

import yaml

from ..utils.errors import FileParseError, DataNotFoundError
from .cache_service import get_cache


class ParserService:
    """数据解析服务类"""

    def __init__(self, project_root: str = None):
        """
        初始化解析服务

        Args:
            project_root: 项目根目录，默认为当前目录的父目录
        """
        if project_root is None:
            current_file = Path(__file__)
            self.project_root = current_file.parent.parent.parent
        else:
            self.project_root = Path(project_root)

        self.cache = get_cache()

    @staticmethod
    def clean_title(title: str) -> str:
        """清理标题文本"""
        title = re.sub(r'\s+', ' ', title)
        title = title.strip()
        return title

    def get_date_folder_name(self, date: datetime = None) -> str:
        """
        获取日期字符串（ISO 格式）

        Args:
            date: 日期对象，默认为今天

        Returns:
            日期字符串（YYYY-MM-DD）
        """
        if date is None:
            date = datetime.now()
        return date.strftime("%Y-%m-%d")

    def _get_db_path(self, date: datetime = None, db_type: str = "news") -> Optional[Path]:
        """
        获取数据库文件路径

        新结构：output/{type}/{date}.db

        Args:
            date: 日期对象，默认为今天
            db_type: 数据库类型 ("news" 或 "rss")

        Returns:
            数据库文件路径，如果不存在则返回 None
        """
        date_str = self.get_date_folder_name(date)
        db_path = self.project_root / "output" / db_type / f"{date_str}.db"
        if db_path.exists():
            return db_path
        return None

    def _read_from_sqlite(
        self,
        date: datetime = None,
        platform_ids: Optional[List[str]] = None,
        db_type: str = "news"
    ) -> Optional[Tuple[Dict, Dict, Dict]]:
        """
        从 SQLite 数据库读取数据

        Args:
            date: 日期对象，默认为今天
            platform_ids: 平台ID列表，None表示所有平台
            db_type: 数据库类型 ("news" 或 "rss")

        Returns:
            (all_titles, id_to_name, all_timestamps) 元组，如果数据库不存在返回 None
        """
        db_path = self._get_db_path(date, db_type)
        if db_path is None:
            return None

        all_titles = {}
        id_to_name = {}
        all_timestamps = {}

        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if db_type == "news":
                return self._read_news_from_sqlite(cursor, platform_ids, all_titles, id_to_name, all_timestamps)
            elif db_type == "rss":
                return self._read_rss_from_sqlite(cursor, platform_ids, all_titles, id_to_name, all_timestamps)

        except Exception as e:
            print(f"Warning: 从 SQLite 读取数据失败: {e}")
            return None
        finally:
            if 'conn' in locals():
                conn.close()

    def _read_news_from_sqlite(
        self,
        cursor,
        platform_ids: Optional[List[str]],
        all_titles: Dict,
        id_to_name: Dict,
        all_timestamps: Dict
    ) -> Optional[Tuple[Dict, Dict, Dict]]:
        """从热榜数据库读取数据"""
        # 检查表是否存在
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='news_items'
        """)
        if not cursor.fetchone():
            return None

        # 构建查询
        if platform_ids:
            placeholders = ','.join(['?' for _ in platform_ids])
            query = f"""
                SELECT n.id, n.platform_id, p.name as platform_name, n.title,
                       n.rank, n.url, n.mobile_url,
                       n.first_crawl_time, n.last_crawl_time, n.crawl_count
                FROM news_items n
                LEFT JOIN platforms p ON n.platform_id = p.id
                WHERE n.platform_id IN ({placeholders})
            """
            cursor.execute(query, platform_ids)
        else:
            cursor.execute("""
                SELECT n.id, n.platform_id, p.name as platform_name, n.title,
                       n.rank, n.url, n.mobile_url,
                       n.first_crawl_time, n.last_crawl_time, n.crawl_count
                FROM news_items n
                LEFT JOIN platforms p ON n.platform_id = p.id
            """)

        rows = cursor.fetchall()

        # 收集所有 news_item_id 用于查询历史排名
        news_ids = [row['id'] for row in rows]
        rank_history_map = {}

        if news_ids:
            placeholders = ",".join("?" * len(news_ids))
            cursor.execute(f"""
                SELECT news_item_id, rank FROM rank_history
                WHERE news_item_id IN ({placeholders})
                ORDER BY news_item_id, crawl_time
            """, news_ids)

            for rh_row in cursor.fetchall():
                news_id = rh_row['news_item_id']
                rank = rh_row['rank']
                if news_id not in rank_history_map:
                    rank_history_map[news_id] = []
                rank_history_map[news_id].append(rank)

        for row in rows:
            news_id = row['id']
            platform_id = row['platform_id']
            platform_name = row['platform_name'] or platform_id
            title = row['title']

            if platform_id not in id_to_name:
                id_to_name[platform_id] = platform_name

            if platform_id not in all_titles:
                all_titles[platform_id] = {}

            ranks = rank_history_map.get(news_id, [row['rank']])

            all_titles[platform_id][title] = {
                "ranks": ranks,
                "url": row['url'] or "",
                "mobileUrl": row['mobile_url'] or "",
                "first_time": row['first_crawl_time'] or "",
                "last_time": row['last_crawl_time'] or "",
                "count": row['crawl_count'] or 1,
            }

        # 获取抓取时间作为 timestamps
        cursor.execute("""
            SELECT crawl_time, created_at FROM crawl_records
            ORDER BY crawl_time
        """)
        for row in cursor.fetchall():
            crawl_time = row['crawl_time']
            created_at = row['created_at']
            try:
                ts = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").timestamp()
            except (ValueError, TypeError):
                ts = datetime.now().timestamp()
            all_timestamps[f"{crawl_time}.db"] = ts

        if not all_titles:
            return None

        return (all_titles, id_to_name, all_timestamps)

    def _read_rss_from_sqlite(
        self,
        cursor,
        feed_ids: Optional[List[str]],
        all_items: Dict,
        id_to_name: Dict,
        all_timestamps: Dict
    ) -> Optional[Tuple[Dict, Dict, Dict]]:
        """从 RSS 数据库读取数据"""
        # 检查表是否存在
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='rss_items'
        """)
        if not cursor.fetchone():
            return None

        # 构建查询
        if feed_ids:
            placeholders = ','.join(['?' for _ in feed_ids])
            query = f"""
                SELECT i.id, i.feed_id, f.name as feed_name, i.title,
                       i.url, i.published_at, i.summary, i.author,
                       i.first_crawl_time, i.last_crawl_time, i.crawl_count
                FROM rss_items i
                LEFT JOIN rss_feeds f ON i.feed_id = f.id
                WHERE i.feed_id IN ({placeholders})
                ORDER BY i.published_at DESC
            """
            cursor.execute(query, feed_ids)
        else:
            cursor.execute("""
                SELECT i.id, i.feed_id, f.name as feed_name, i.title,
                       i.url, i.published_at, i.summary, i.author,
                       i.first_crawl_time, i.last_crawl_time, i.crawl_count
                FROM rss_items i
                LEFT JOIN rss_feeds f ON i.feed_id = f.id
                ORDER BY i.published_at DESC
            """)

        rows = cursor.fetchall()

        for row in rows:
            feed_id = row['feed_id']
            feed_name = row['feed_name'] or feed_id
            title = row['title']

            if feed_id not in id_to_name:
                id_to_name[feed_id] = feed_name

            if feed_id not in all_items:
                all_items[feed_id] = {}

            all_items[feed_id][title] = {
                "url": row['url'] or "",
                "published_at": row['published_at'] or "",
                "summary": row['summary'] or "",
                "author": row['author'] or "",
                "first_time": row['first_crawl_time'] or "",
                "last_time": row['last_crawl_time'] or "",
                "count": row['crawl_count'] or 1,
            }

        # 获取抓取时间
        cursor.execute("""
            SELECT crawl_time, created_at FROM rss_crawl_records
            ORDER BY crawl_time
        """)
        for row in cursor.fetchall():
            crawl_time = row['crawl_time']
            created_at = row['created_at']
            try:
                ts = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").timestamp()
            except (ValueError, TypeError):
                ts = datetime.now().timestamp()
            all_timestamps[f"{crawl_time}.db"] = ts

        if not all_items:
            return None

        return (all_items, id_to_name, all_timestamps)

    def read_all_titles_for_date(
        self,
        date: datetime = None,
        platform_ids: Optional[List[str]] = None,
        db_type: str = "news"
    ) -> Tuple[Dict, Dict, Dict]:
        """
        读取指定日期的所有数据（带缓存）

        Args:
            date: 日期对象，默认为今天
            platform_ids: 平台/Feed ID列表，None表示所有
            db_type: 数据库类型 ("news" 或 "rss")

        Returns:
            (all_titles, id_to_name, all_timestamps) 元组

        Raises:
            DataNotFoundError: 数据不存在
        """
        date_str = self.get_date_folder_name(date)
        platform_key = ','.join(sorted(platform_ids)) if platform_ids else 'all'
        cache_key = f"read_all:{db_type}:{date_str}:{platform_key}"

        is_today = (date is None) or (date.date() == datetime.now().date())
        ttl = 900 if is_today else 3600

        cached = self.cache.get(cache_key, ttl=ttl)
        if cached:
            return cached

        result = self._read_from_sqlite(date, platform_ids, db_type)
        if result:
            self.cache.set(cache_key, result)
            return result

        raise DataNotFoundError(
            f"未找到 {date_str} 的 {db_type} 数据",
            suggestion="请先运行爬虫或检查日期是否正确"
        )

    def parse_yaml_config(self, config_path: str = None) -> dict:
        """
        解析YAML配置文件

        Args:
            config_path: 配置文件路径，默认为 config/config.yaml

        Returns:
            配置字典

        Raises:
            FileParseError: 配置文件解析错误
        """
        if config_path is None:
            config_path = self.project_root / "config" / "config.yaml"
        else:
            config_path = Path(config_path)

        if not config_path.exists():
            raise FileParseError(str(config_path), "配置文件不存在")

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = yaml.safe_load(f)
            return config_data
        except Exception as e:
            raise FileParseError(str(config_path), str(e))

    def _parse_frequency_words_simple(self, words_file: str = None) -> List[Dict]:
        """
        解析关键词配置文件

        复用 trendradar.core.frequency 的解析逻辑，支持：
        - 空行分隔词组
        - +前缀必须词、!前缀过滤词、@数量限制
        - /pattern/ 正则表达式语法
        - => 备注 显示名称语法
        - [GLOBAL_FILTER] 全局过滤区域

        Args:
            words_file: 关键词文件路径，默认为 config/frequency_words.txt

        Returns:
            词组列表

        Raises:
            FileParseError: 文件解析错误
        """
        from trendradar.core.frequency import load_frequency_words

        if words_file is None:
            words_file = str(self.project_root / "config" / "frequency_words.txt")
        else:
            words_file = str(words_file)

        try:
            word_groups, filter_words, global_filters = load_frequency_words(words_file)
            return word_groups
        except FileNotFoundError:
            return []
        except Exception as e:
            raise FileParseError(words_file, str(e))

    def parse_frequency_words(self, words_file: str = None) -> List[Dict]:
        """
        解析关键词配置文件（支持完整语法）

        支持：#名称 @分类、+词/词+、!词/词!、@数字、[GLOBAL_FILTER]、[WORD_GROUPS] 分区
        """
        if words_file is None:
            words_file = self.project_root / "config" / "frequency_words.txt"
        else:
            words_file = Path(words_file)

        if not words_file.exists():
            return []

        try:
            with open(words_file, "r", encoding="utf-8") as f:
                content = f.read()

            blocks = [b.strip() for b in re.split(r"\n\s*\n", content) if b.strip()]
            processed_groups: List[Dict] = []
            current_section = "WORD_GROUPS"

            for block in blocks:
                lines = [line.strip() for line in block.split("\n") if line.strip()]
                if not lines:
                    continue

                if lines[0].startswith("[") and lines[0].endswith("]"):
                    section_name = lines[0][1:-1].upper()
                    if section_name in ("GLOBAL_FILTER", "WORD_GROUPS"):
                        current_section = section_name
                        lines = lines[1:]

                if current_section == "GLOBAL_FILTER":
                    continue

                group = self._parse_word_block(lines)
                if group:
                    processed_groups.append(group)

            return processed_groups
        except Exception as e:
            raise FileParseError(str(words_file), str(e))

    def _parse_word_block(self, lines: List[str]) -> Optional[Dict]:
        """解析单个关键词块"""
        group_required: List[str] = []
        group_normal: List[str] = []
        group_filter: List[str] = []
        group_max_count = 0
        group_name = None
        group_category = "其他"

        def add_token(token: str) -> None:
            token = token.strip()
            if not token:
                return
            if token.startswith("+"):
                w = token[1:].strip()
                if w: group_required.append(w)
            elif token.startswith("!"):
                w = token[1:].strip()
                if w: group_filter.append(w)
            elif token.endswith("+"):
                w = token[:-1].strip()
                if w: group_required.append(w)
            elif token.endswith("!"):
                w = token[:-1].strip()
                if w: group_filter.append(w)
            else:
                group_normal.append(token)

        for line in lines:
            if line.startswith("#"):
                name_part = line[1:].strip()
                if " @" in name_part:
                    name, cat = name_part.split(" @", 1)
                    group_name = name.strip() or None
                    group_category = cat.strip() or group_category
                else:
                    group_name = name_part or None
            elif line.startswith("@"):
                try:
                    c = int(line[1:].strip())
                    if c > 0: group_max_count = c
                except ValueError:
                    pass
            elif "|" in line or "," in line:
                for part in line.split("|"):
                    for t in part.split(","):
                        add_token(t)
            else:
                add_token(line)

        if not group_required and not group_normal:
            return None

        group_key = group_name or (group_normal[0] if group_normal else group_required[0])
        return {
            "required": group_required,
            "normal": group_normal,
            "filter_words": group_filter,
            "group_key": group_key,
            "max_count": group_max_count,
            "category": group_category,
        }

    def get_available_dates(self, db_type: str = "news") -> List[str]:
        """
        获取可用的日期列表

        Args:
            db_type: 数据库类型 ("news" 或 "rss")

        Returns:
            日期字符串列表（YYYY-MM-DD 格式，降序排列）
        """
        db_dir = self.project_root / "output" / db_type
        if not db_dir.exists():
            return []

        dates = []
        for db_file in db_dir.glob("*.db"):
            date_match = re.match(r'(\d{4}-\d{2}-\d{2})\.db$', db_file.name)
            if date_match:
                dates.append(date_match.group(1))

        return sorted(dates, reverse=True)

    def get_available_date_range(self, db_type: str = "news") -> Tuple[Optional[datetime], Optional[datetime]]:
        """
        获取可用的日期范围

        Args:
            db_type: 数据库类型 ("news" 或 "rss")

        Returns:
            (最早日期, 最新日期) 元组，如果没有数据则返回 (None, None)
        """
        dates = self.get_available_dates(db_type)
        if not dates:
            return (None, None)

        earliest = datetime.strptime(dates[-1], "%Y-%m-%d")
        latest = datetime.strptime(dates[0], "%Y-%m-%d")
        return (earliest, latest)
