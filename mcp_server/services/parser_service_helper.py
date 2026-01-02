"""
关键词解析扩展模块

将扩展的关键词解析逻辑独立出来，减少对 parser_service.py 的修改，
降低与上游合并时的冲突风险。
"""

import re
from pathlib import Path
from typing import Dict, List, Optional


def parse_frequency_words_extended(words_file, project_root) -> List[Dict]:
    """
    解析关键词配置文件（支持完整语法）

    支持：#名称 @分类、+词/词+、!词/词!、@数字、[GLOBAL_FILTER]、[WORD_GROUPS] 分区

    Args:
        words_file: 关键词文件路径，None 则使用默认路径
        project_root: 项目根目录

    Returns:
        词组列表
    """
    if words_file is None:
        words_file = Path(project_root) / "config" / "frequency_words.txt"
    else:
        words_file = Path(words_file)

    if not words_file.exists():
        return []

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

        group = _parse_word_block(lines)
        if group:
            processed_groups.append(group)

    return processed_groups


def _parse_word_block(lines: List[str]) -> Optional[Dict]:
    """解析单个关键词块"""
    group_required: List[str] = []
    group_normal: List[str] = []
    group_filter: List[str] = []
    group_max_count = 0
    group_name = None
    group_category = "其他"

    def add_token(token: str) -> None:
        nonlocal group_required, group_normal, group_filter
        token = token.strip()
        if not token:
            return
        if token.startswith("+"):
            w = token[1:].strip()
            if w:
                group_required.append(w)
        elif token.startswith("!"):
            w = token[1:].strip()
            if w:
                group_filter.append(w)
        elif token.endswith("+"):
            w = token[:-1].strip()
            if w:
                group_required.append(w)
        elif token.endswith("!"):
            w = token[:-1].strip()
            if w:
                group_filter.append(w)
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
                if c > 0:
                    group_max_count = c
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
