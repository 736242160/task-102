#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cascade_resolver.py — 样式层叠解析工具（纯 Python 标准库，单文件）

功能
----
1. 读取样式规则流（JSON）：每条规则包含来源等级（author / user / default）、
   选择器（标签、类、嵌套路径）与属性键值；
2. 针对目标元素（以其祖先链描述）计算每个属性的最终生效值：
   - 多来源规则按来源等级确定基础优先级：default < user < author；
   - 同一来源等级内按规则出现顺序，后出现者优先；
   - 同一属性被多条规则命中（冲突）时按上述优先级裁决；
   - 最高优先级相同且值不同（同权冲突）时，按出现顺序取后者并报告冲突；
   - 可继承属性（如 color、font-size 等）沿父链向子元素传播；
3. 校验选择器格式（报告规则序号与字符位置）、属性键与属性值的合法性；
4. 输出每个属性的最终生效值与错误清单（JSON）。

用法
----
    python3 cascade_resolver.py input.json          # 从文件读取输入
    cat input.json | python3 cascade_resolver.py    # 从标准输入读取
    python3 cascade_resolver.py --demo              # 运行内置示例

输入格式（JSON）
---------------
{
  "rules": [
    {"origin": "author", "selector": "div .note",
     "properties": {"color": "red", "margin": "8px"}}
  ],
  "target": [                       // 目标元素的祖先链，最后一项为目标元素本身
    {"tag": "html"},
    {"tag": "body"},
    {"tag": "div", "classes": ["note"]}
  ]
}

选择器语法
----------
- 标签选择器：        div
- 类选择器：          .note
- 标签 + 类：         div.note（可多个类：div.a.b）
- 通配标签：          *  或  *.note
- 嵌套路径（后代）：  html body div.note p   （空格分隔，右起匹配）
"""

import argparse
import json
import re
import sys

# ---------------------------------------------------------------------------
# 常量与注册表
# ---------------------------------------------------------------------------

# 来源等级 -> 基础优先级（数值越大优先级越高）
ORIGIN_LEVELS = {"default": 0, "user": 1, "author": 2}

# 可继承属性集合：元素自身未命中时，从父元素的计算值继承
INHERITED_PROPERTIES = {
    "color", "font-size", "font-family", "line-height", "text-align",
    "visibility",
}

_IDENT_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")
_SIZE_RE = re.compile(r"^(0|[0-9]+(\.[0-9]+)?(px|em|rem|pt|%))$")
_HEX_COLOR_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_RGB_FUNC_RE = re.compile(
    r"^rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*"
    r"(,\s*(0|1|0?\.\d+)\s*)?\)$"
)

_NAMED_COLORS = {
    "black", "white", "red", "green", "blue", "yellow", "orange", "purple",
    "pink", "brown", "gray", "grey", "cyan", "magenta", "silver", "maroon",
    "olive", "navy", "teal", "lime", "aqua", "fuchsia", "transparent",
}


def _is_color(value):
    return (
        value in _NAMED_COLORS
        or bool(_HEX_COLOR_RE.match(value))
        or bool(_RGB_FUNC_RE.match(value))
    )


def _is_size(value):
    return bool(_SIZE_RE.match(value))


def _is_box(value):
    """1~4 个尺寸 token，如 '8px' 或 '8px 4px'。"""
    parts = value.split()
    return 1 <= len(parts) <= 4 and all(_is_size(p) for p in parts)


def _enum(*options):
    return lambda value: value in options


# 属性注册表：属性键 -> 值校验器
PROPERTY_SPECS = {
    "color": _is_color,
    "background-color": _is_color,
    "font-size": _is_size,
    "font-family": lambda v: bool(v.strip()) and not any(
        c in v for c in "{};<>"),
    "line-height": lambda v: _is_size(v) or bool(
        re.match(r"^[0-9]+(\.[0-9]+)?$", v)),
    "text-align": _enum("left", "right", "center", "justify"),
    "display": _enum("block", "inline", "inline-block", "none", "flex",
                     "grid"),
    "visibility": _enum("visible", "hidden", "collapse"),
    "margin": _is_box,
    "padding": _is_box,
    "width": lambda v: _is_size(v) or v == "auto",
    "height": lambda v: _is_size(v) or v == "auto",
    "border-width": lambda v: _is_size(v) or v in ("thin", "medium", "thick"),
}


# ---------------------------------------------------------------------------
# 错误工具
# ---------------------------------------------------------------------------

def _error(errors, rule_index, err_type, message, position=None,
           property_name=None):
    entry = {"rule": rule_index, "type": err_type, "message": message}
    if position is not None:
        entry["position"] = position
    if property_name is not None:
        entry["property"] = property_name
    errors.append(entry)


# ---------------------------------------------------------------------------
# 选择器解析与匹配
# ---------------------------------------------------------------------------

class Compound:
    """复合选择器：可选标签 + 若干类。"""

    __slots__ = ("tag", "classes")

    def __init__(self, tag, classes):
        self.tag = tag          # None 表示任意标签（* 或纯类选择器）
        self.classes = classes  # list[str]

    def matches(self, element):
        if self.tag is not None and self.tag != element.get("tag"):
            return False
        own = element.get("classes", [])
        return all(c in own for c in self.classes)

    def __repr__(self):
        return (self.tag or "*") + "".join("." + c for c in self.classes)


def parse_selector(selector, rule_index, errors):
    """解析选择器为 Compound 列表；非法时记录错误（含字符位置）并返回 None。"""
    if not isinstance(selector, str) or not selector.strip():
        _error(errors, rule_index, "invalid-selector",
               "选择器为空或不是字符串")
        return None

    compounds = []
    for m in re.finditer(r"\S+", selector):
        token, base = m.group(), m.start()
        comp = _parse_compound(token, base, rule_index, errors)
        if comp is None:
            return None
        compounds.append(comp)
    return compounds or None


def _parse_compound(token, base, rule_index, errors):
    """解析单个复合选择器 token，base 为 token 在选择器串中的起始偏移。"""
    rest = token
    rest_base = base
    tag = None

    if not rest.startswith("."):
        dot = rest.find(".")
        tag_part = rest if dot == -1 else rest[:dot]
        if tag_part == "*":
            tag = None  # 通配标签
        elif _IDENT_RE.match(tag_part):
            tag = tag_part
        else:
            _error(errors, rule_index, "invalid-selector",
                   "非法标签名 %r" % tag_part, position=rest_base)
            return None
        if dot == -1:
            return Compound(tag, [])
        rest = rest[dot:]
        rest_base = base + dot

    # 此时 rest 形如 '.a.b.c'
    classes = []
    i = 0
    while i < len(rest):
        # rest[i] 必为 '.'
        j = i + 1
        start = j
        while j < len(rest) and rest[j] != ".":
            j += 1
        name = rest[start:j]
        if not _IDENT_RE.match(name):
            _error(errors, rule_index, "invalid-selector",
                   "非法类名 %r" % (name or "(空)"),
                   position=rest_base + start)
            return None
        classes.append(name)
        i = j
    return Compound(tag, classes)


def selector_matches(compounds, path):
    """嵌套路径匹配：最右复合选择器匹配目标元素，其余从右向左在祖先中
    按后代关系（不必相邻）依次匹配。path 为祖先链，最后一项为目标。"""
    if not compounds[-1].matches(path[-1]):
        return False
    i = len(path) - 2
    for comp in reversed(compounds[:-1]):
        while i >= 0 and not comp.matches(path[i]):
            i -= 1
        if i < 0:
            return False
        i -= 1
    return True


# ---------------------------------------------------------------------------
# 规则解析
# ---------------------------------------------------------------------------

class Declaration:
    """一条属性声明：某规则中某属性的键值及其优先级信息。"""

    __slots__ = ("prop", "value", "origin", "origin_level", "order",
                 "rule_index", "selector")

    def __init__(self, prop, value, origin, order, rule_index, selector):
        self.prop = prop
        self.value = value
        self.origin = origin
        self.origin_level = ORIGIN_LEVELS[origin]
        self.order = order            # 规则出现顺序（规则序号）
        self.rule_index = rule_index
        self.selector = selector      # 已解析的 Compound 列表


def parse_rules(rules, errors):
    """解析规则流，返回 Declaration 列表；非法项记入 errors。"""
    declarations = []
    for idx, rule in enumerate(rules):
        if not isinstance(rule, dict):
            _error(errors, idx, "invalid-rule", "规则不是对象")
            continue

        origin = rule.get("origin")
        if origin not in ORIGIN_LEVELS:
            _error(errors, idx, "invalid-origin",
                   "非法来源等级 %r（应为 author/user/default）" % (origin,))
            continue

        selector = parse_selector(rule.get("selector"), idx, errors)
        if selector is None:
            continue

        props = rule.get("properties")
        if not isinstance(props, dict) or not props:
            _error(errors, idx, "invalid-rule",
                   "properties 缺失或不是非空对象")
            continue

        for key, raw_value in props.items():
            if key not in PROPERTY_SPECS:
                _error(errors, idx, "invalid-property",
                       "未知属性键 %r" % (key,), property_name=key)
                continue
            if not isinstance(raw_value, (str, int, float)):
                _error(errors, idx, "invalid-value",
                       "属性值必须是字符串或数字", property_name=key)
                continue
            value = str(raw_value).strip()
            if not PROPERTY_SPECS[key](value):
                _error(errors, idx, "invalid-value",
                       "属性 %r 的非法值 %r" % (key, value),
                       property_name=key)
                continue
            declarations.append(
                Declaration(key, value, origin, idx, idx, selector))
    return declarations


# ---------------------------------------------------------------------------
# 层叠裁决与继承
# ---------------------------------------------------------------------------

def _cascade_element(declarations, path, depth, conflicts):
    """计算单个元素的层叠值（不含继承）。返回 {prop: Declaration}。"""
    by_prop = {}
    for decl in declarations:
        if selector_matches(decl.selector, path):
            by_prop.setdefault(decl.prop, []).append(decl)

    winners = {}
    for prop, decls in by_prop.items():
        # 裁决键：先来源等级，后出现顺序
        winner = max(decls, key=lambda d: (d.origin_level, d.order))
        winners[prop] = winner
        # 同权冲突：与胜者同来源等级、值却不同的命中声明
        rivals = [d for d in decls
                  if d.origin_level == winner.origin_level
                  and d.value != winner.value]
        if rivals:
            conflicts.append({
                "element": depth,
                "property": prop,
                "origin": winner.origin,
                "candidates": sorted(
                    [{"rule": d.rule_index, "value": d.value}
                     for d in decls
                     if d.origin_level == winner.origin_level],
                    key=lambda c: c["rule"]),
                "winner": {"rule": winner.rule_index, "value": winner.value},
                "resolution": "同权冲突，按出现顺序取后出现的规则",
            })
    return winners


def resolve_styles(rules, target):
    """主入口：返回 {values, conflicts, errors}。"""
    errors, conflicts = [], []

    if not isinstance(rules, list):
        return {"values": {}, "conflicts": [],
                "errors": [{"rule": None, "type": "invalid-input",
                            "message": "rules 必须是数组"}]}
    if (not isinstance(target, list) or not target
            or any(not isinstance(e, dict)
                   or not isinstance(e.get("tag"), str) for e in target)):
        return {"values": {}, "conflicts": [],
                "errors": [{"rule": None, "type": "invalid-input",
                            "message": "target 必须是非空数组，"
                                       "每项含字符串 tag"}]}

    declarations = parse_rules(rules, errors)

    # 沿祖先链逐层计算：层叠值 + 继承传播
    parent_final = {}
    final = {}
    for depth in range(len(target)):
        path = target[:depth + 1]
        winners = _cascade_element(declarations, path, depth, conflicts)

        final = {}
        for prop, decl in winners.items():
            final[prop] = {
                "value": decl.value,
                "source": "cascade",
                "rule": decl.rule_index,
                "origin": decl.origin,
            }
        # 继承：自身未命中的可继承属性取父元素计算值
        for prop, info in parent_final.items():
            if prop in INHERITED_PROPERTIES and prop not in final:
                final[prop] = {
                    "value": info["value"],
                    "source": "inherited",
                    "from": depth - 1,
                    "rule": info["rule"],
                    "origin": info["origin"],
                }
        parent_final = final

    return {"values": final, "conflicts": conflicts, "errors": errors}


# ---------------------------------------------------------------------------
# 内置示例
# ---------------------------------------------------------------------------

DEMO_INPUT = {
    "rules": [
        {"origin": "default", "selector": "p",
         "properties": {"font-size": "12px", "color": "#888888",
                        "display": "block"}},
        {"origin": "user", "selector": "p",
         "properties": {"color": "blue"}},
        {"origin": "author", "selector": "div p",
         "properties": {"color": "red"}},
        {"origin": "author", "selector": "body p",
         "properties": {"color": "green"}},          # 与规则 2 同权冲突
        {"origin": "author", "selector": ".note",
         "properties": {"text-align": "center",
                        "font-family": "Georgia, serif",
                        "margin": "8px"}},
        {"origin": "author", "selector": "div .note",
         "properties": {"margin": "10px"}},          # 与规则 4 同权冲突
        {"origin": "author", "selector": "div..note",
         "properties": {"color": "black"}},          # 非法选择器
        {"origin": "author", "selector": "p",
         "properties": {"colour": "red",             # 非法属性键
                        "font-size": "huge"}},       # 非法属性值
    ],
    "target": [
        {"tag": "html"},
        {"tag": "body"},
        {"tag": "div"},
        {"tag": "div", "classes": ["note"]},
        {"tag": "p"},
    ],
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="样式层叠解析工具：计算目标元素各属性的最终生效值")
    parser.add_argument("input", nargs="?",
                        help="输入 JSON 文件（缺省读标准输入）")
    parser.add_argument("--demo", action="store_true",
                        help="运行内置示例")
    args = parser.parse_args(argv)

    if args.demo:
        data = DEMO_INPUT
        print("=== 输入 ===")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print("=== 输出 ===")
    else:
        try:
            text = (open(args.input, encoding="utf-8").read()
                    if args.input else sys.stdin.read())
            data = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps(
                {"values": {}, "conflicts": [],
                 "errors": [{"rule": None, "type": "invalid-input",
                             "message": "输入不是合法 JSON：%s" % exc}]},
                ensure_ascii=False, indent=2))
            return 1

    if not isinstance(data, dict):
        result = {"values": {}, "conflicts": [],
                  "errors": [{"rule": None, "type": "invalid-input",
                              "message": "输入必须是含 rules/target 的对象"}]}
    else:
        result = resolve_styles(data.get("rules", []), data.get("target"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
