# coding=utf-8
"""
频率词配置扩展模块

在上游 frequency.py 基础上，增加对 #主题名 @分类名 语法的支持。
这个文件是你自己的扩展，不会和上游冲突。

功能：
- 解析 #主题名 @分类名 语法（上游不支持）
- 调用上游的 _parse_word() 解析正则和显示名称
- 调用上游的 _word_matches() 进行匹配
- 提供 find_matched_topics() 给 api_server.py 使用
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Union

# 导入上游的函数
import sys
sys.path.insert(0, '/app')
try:
    from trendradar.core.frequency import _parse_word, _word_matches
    UPSTREAM_AVAILABLE = True
except ImportError:
    UPSTREAM_AVAILABLE = False
    print("Warning: trendradar.core.frequency not available, using fallback")


def _parse_word_fallback(word: str) -> Dict:
    """
    回退解析函数，当上游不可用时使用
    """
    display_name = None
    
    # 解析 => 显示名称 语法
    display_match = re.search(r'\s*=>\s*', word)
    if display_match:
        parts = re.split(r'\s*=>\s*', word, 1)
        word = parts[0].strip()
        display_name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    
    # 解析正则表达式
    regex_match = re.match(r'^/(.+)/([gimsux]*)$', word)
    if regex_match:
        pattern_str = regex_match.group(1)
        try:
            pattern = re.compile(pattern_str, re.IGNORECASE)
            return {
                "word": pattern_str,
                "is_regex": True,
                "pattern": pattern,
                "display_name": display_name,
            }
        except re.error:
            pass
    
    return {"word": word, "is_regex": False, "pattern": None, "display_name": display_name}


def _word_matches_fallback(word_config: Union[str, Dict], title_lower: str) -> bool:
    """
    回退匹配函数，当上游不可用时使用
    """
    if isinstance(word_config, str):
        return word_config.lower() in title_lower
    
    if word_config.get("is_regex") and word_config.get("pattern"):
        return bool(word_config["pattern"].search(title_lower))
    else:
        return word_config["word"].lower() in title_lower


# 选择使用上游还是回退函数
parse_word = _parse_word if UPSTREAM_AVAILABLE else _parse_word_fallback
word_matches = _word_matches if UPSTREAM_AVAILABLE else _word_matches_fallback


def load_frequency_words_extended(
    frequency_file: Optional[str] = None,
) -> Tuple[List[Dict], List[Dict], List[str]]:
    """
    加载频率词配置（扩展版，支持 #主题名 @分类名 语法）
    
    配置文件格式说明：
    - 每个词组由空行分隔
    - [GLOBAL_FILTER] 区域定义全局过滤词
    - [WORD_GROUPS] 区域定义词组（默认）
    
    词组语法：
    - #主题名 @分类名：定义主题名称和分类（你的扩展语法）
    - 普通词：直接写入，任意匹配即可
    - /pattern/ 或 /pattern/i：正则表达式（上游语法）
    - => 显示名称：给关键词起别名（上游语法）
    - +词：必须词，所有必须词都要匹配
    - !词：过滤词，匹配则排除
    - @数字：该词组最多显示的条数
    
    Args:
        frequency_file: 频率词配置文件路径
        
    Returns:
        (词组列表, 过滤词列表, 全局过滤词列表)
    """
    if frequency_file is None:
        frequency_file = os.environ.get(
            "FREQUENCY_WORDS_PATH", "config/frequency_words.txt"
        )
    
    frequency_path = Path(frequency_file)
    if not frequency_path.exists():
        raise FileNotFoundError(f"频率词文件 {frequency_file} 不存在")
    
    with open(frequency_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    word_groups = [group.strip() for group in content.split("\n\n") if group.strip()]
    
    processed_groups = []
    filter_words = []
    global_filters = []
    
    current_section = "WORD_GROUPS"
    
    for group in word_groups:
        lines = [line.strip() for line in group.split("\n") if line.strip()]
        
        if not lines:
            continue
        
        # 检查是否为区域标记
        if lines[0].startswith("[") and lines[0].endswith("]"):
            section_name = lines[0][1:-1].upper()
            if section_name in ("GLOBAL_FILTER", "WORD_GROUPS"):
                current_section = section_name
                lines = lines[1:]
        
        # 处理全局过滤区域
        if current_section == "GLOBAL_FILTER":
            for line in lines:
                if line.startswith(("!", "+", "@", "#")):
                    continue
                if line:
                    global_filters.append(line)
            continue
        
        # 处理词组区域
        words = lines
        
        group_required_words = []
        group_normal_words = []
        group_filter_words = []
        group_max_count = 0
        group_name = None  # 自定义主题名称（你的扩展）
        group_category = "其他"  # 默认分类（你的扩展）
        
        for word in words:
            if word.startswith("#"):
                # 你的扩展语法：#主题名 @分类名
                name_part = word[1:].strip()
                if " @" in name_part:
                    parts = name_part.split(" @", 1)
                    group_name = parts[0].strip()
                    group_category = parts[1].strip()
                else:
                    group_name = name_part
            elif word.startswith("@"):
                # 解析最大显示数量
                try:
                    count = int(word[1:])
                    if count > 0:
                        group_max_count = count
                except (ValueError, IndexError):
                    pass
            elif word.startswith("!"):
                # 过滤词（支持正则语法）
                filter_word = word[1:]
                parsed = parse_word(filter_word)
                filter_words.append(parsed)
                group_filter_words.append(parsed)
            elif word.startswith("+"):
                # 必须词（支持正则语法）
                req_word = word[1:]
                group_required_words.append(parse_word(req_word))
            else:
                # 普通词（支持正则语法）
                group_normal_words.append(parse_word(word))
        
        if group_required_words or group_normal_words:
            # 确定 group_key（主题名）
            if group_name:
                group_key = group_name
            else:
                # 使用第一个有 display_name 的词，或第一个词
                for w in group_normal_words + group_required_words:
                    if w.get("display_name"):
                        group_key = w["display_name"]
                        break
                else:
                    if group_normal_words:
                        group_key = group_normal_words[0].get("display_name") or group_normal_words[0]["word"]
                    else:
                        group_key = group_required_words[0].get("display_name") or group_required_words[0]["word"]
            
            # 提取关键词列表（用于前端显示）
            keywords = []
            for w in group_normal_words + group_required_words:
                display = w.get("display_name") or w["word"]
                if display not in keywords:
                    keywords.append(display)
            
            processed_groups.append({
                "required": group_required_words,
                "normal": group_normal_words,
                "filter": group_filter_words,  # 词组内过滤词
                "group_key": group_key,
                "max_count": group_max_count,
                "category": group_category,  # 你的扩展
                "keywords": keywords,  # 用于前端显示
            })
    
    return processed_groups, filter_words, global_filters


def find_matched_topics(
    title: str, 
    word_groups: List[Dict], 
    filter_words: List[Dict], 
    global_filters: List[str]
) -> List[Dict]:
    """
    找出标题匹配的所有主题和关键词
    
    Args:
        title: 新闻标题
        word_groups: 词组列表
        filter_words: 过滤词列表
        global_filters: 全局过滤词列表
        
    Returns:
        匹配结果列表: [{"topic": "主题名", "matched": ["关键词1", "关键词2"]}, ...]
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
        if word_matches(fw, title_lower):
            return []
    
    matched_topics = []
    
    for group in word_groups:
        required_words = group.get("required", [])
        normal_words = group.get("normal", [])
        group_key = group.get("group_key", "")
        group_filter = group.get("filter", [])
        
        # 词组内过滤词检查
        skip_group = False
        for fw in group_filter:
            if word_matches(fw, title_lower):
                skip_group = True
                break
        if skip_group:
            continue
        
        # 必须词检查
        if required_words:
            all_required = all(word_matches(rw, title_lower) for rw in required_words)
            if not all_required:
                continue
        
        # 普通词检查 - 找出所有匹配的词
        matched_words = []
        if normal_words:
            for nw in normal_words:
                if word_matches(nw, title_lower):
                    # 使用 display_name 或原词
                    display = nw.get("display_name") or nw["word"]
                    if display not in matched_words:
                        matched_words.append(display)
            if not matched_words:
                continue
        
        # 添加必须词到匹配列表
        if required_words:
            for rw in required_words:
                display = rw.get("display_name") or rw["word"]
                if display not in matched_words:
                    matched_words.insert(0, display)
        
        matched_topics.append({
            "topic": group_key,
            "matched": matched_words[:3]  # 最多显示3个匹配词
        })
    
    return matched_topics
