"""前置过滤：数据 / 代码类世界书条目**根本不送 LLM**（设计文档 §7）。

这是四层防御的第一层，也是唯一零成本的一层：

| 层 | 作用 | 成本 |
|---|---|---|
| ① 前置过滤（本模块） | 命中就不进批次 | 0（纯正则/解析） |
| ② 检查器（`wicheck`） | 送出去也拦 | 一次调用 |
| ③ 落地策略 | 决定默认是否写回 | 0 |
| ④ 提示词 | 只负责「做得更好」 | — |

**为什么后置检查兜不住批量**：上一轮实测 62 条里 7 条被改坏，其中 4 条仍判「通过」。
检查器是逐条启发式，兜不住「结构没写在名字里」的整类条目；而想拦住它们，
最便宜的办法就是**不送**。

判定顺序（照 §7，实测校准过）：

1. **名字信号**：`initvar`、`[核心]/[机制]/[MVU]/…` 前缀。精度极高（29 条里 18 条
   旧标记完全抓不到），但**不能只靠名字**——13 张 initvar 卡里有 4 张条目名就是裸
   `[initvar]`，看着像占位垃圾，内容却是 229–1,734 字的变量树。
2. **结构化树**：能被 `yaml.safe_load` 解析成 dict/list 且「大或嵌套」。
   名字能骗人，结构不会。
3. **INI 段**：3 段以上 `[section]`，专治 MVU 与状态栏模板。
4. **旧标记 + 代码构造**：宏 / EJS / `<script>` / 布局 HTML / STscript / 变量函数 /
   JS 特征 / Python / JS / JSON 块。

全库实测命中率（2,557 条）：旧标记 44.8%，加代码构造 47.2%，再加 INI/结构化约 48%。

**命中不是永久的**：作业参数 `force=True` 可以强制处理（面板里单独勾选），
过滤只是「默认不送」。这与「检查器永不禁止手动采纳」是同一条原则。
"""

import re

#: 长度上限：超过这个长度的内容不做 YAML 解析。
#: 不是为了「跳过判断」，而是 `yaml` 支持锚点/别名，超长输入有放大风险；
#: 世界书条目极少超过 10 万字，超过的基本都是长篇正文而非结构化数据。
MAX_STRUCTURED_SCAN = 100_000

#: 名字信号（精度最高，第一个判）
STRICT_NAME = (
    re.compile(r'initvar', re.I),
    re.compile(
        r'^[\[【](?:MVU|核心|机制|系统|规则|变量|状态栏|模板|开局|格式|指令|随机)[\]】]',
    ),
)

#: INI / 状态栏模板块的段标记（独占一行的 `[xxx]`）
INI_SECTION = re.compile(r'(?m)^[ \t]*\[[^\[\]\n]{1,40}\][ \t]*$')

#: 旧标记：宏 / EJS / script / 布局 HTML / STscript / 变量函数 / JS 特征
_LEGACY_MARKERS = (
    ('macro', re.compile(r'\{\{.*?\}\}', re.S)),
    ('ejs', re.compile(r'<%.*?%>', re.S)),
    ('script', re.compile(r'</?script\b', re.I)),
    (
        'html',
        re.compile(
            r'<(/?)(div|span|details|summary|table|tr|td|th|p|br|b|i|em|strong|'
            r'ul|ol|li|h[1-6]|code|pre|blockquote|font|img|a)\b',
            re.I,
        ),
    ),
    (
        'stscript',
        re.compile(
            r'(^|\n)[ \t]*/(?:setvar|getvar|addvar|incvar|decvar|setglobalvar|'
            r'getglobalvar|trigger|sys|send|comment|pass|run|if|else|while|input|'
            r'buttons|choice|random|pick)\b',
        ),
    ),
    ('var_func', re.compile(r'\{\{(?:get|set|add|inc|dec|getglobal|setglobal)[^}]*\}\}', re.I)),
    ('js_code', re.compile(r'(function\s*\(|=>|document\.|window\.|localStorage)', re.I)),
)

#: 代码构造：补上「没有任何宏/EJS/HTML、只有裸代码」的条目
#: （实测 `食谱大师` / `圣女外貌描写` 就是这么被改坏的）。
_CODE_MARKERS = (
    (
        'code_py',
        re.compile(
            r'(^|\n)[ \t]*(def\s+\w+\s*\(|class\s+\w+\s*[:\(]|async\s+def\s+|@\w+\s*\()|'
            r'\bself\.\w+|\b(?:import|from)\s+[A-Za-z_][\w.]*\s*(?:import|$)',
            re.M,
        ),
    ),
    (
        'code_js',
        re.compile(
            r'\bfunction\s*\w*\s*\(|\bconsole\.(?:log|error|warn|info)\s*\(|'
            r'\b(?:const|let|var)\s+\w+\s*=|\bawait\s+|\basync\s+\(|'
            r'\b\w+\.(?:push|forEach|map|filter)\s*\(',
            re.I,
        ),
    ),
    ('json_block', re.compile(r'(^|\n)[ \t]*\{[ \t]*"[\w\u4e00-\u9fa5]+"[ \t]*:', re.M)),
)

PROTECT_PATTERNS = _LEGACY_MARKERS + _CODE_MARKERS

#: 结构化树的判定阈值：≥6 个顶层键，或 ≥3 个键且存在嵌套
STRUCTURED_MIN_KEYS = 6
STRUCTURED_NESTED_MIN_KEYS = 3


def _text(value) -> str:
    return '' if value is None else str(value)


def _yaml_tree(text: str):
    """尽量把文本解析成 YAML 树；解析不了（或没有 pyyaml）返回 `None`。

    这里**只用于判断「是不是结构化数据」**，不参与改写结果的校验——
    校验侧仍然坚持「不做 YAML 解析」（见 `wicheck` 模块 docstring）。
    """
    if not text or len(text) > MAX_STRUCTURED_SCAN:
        return None
    try:
        import yaml  # noqa: PLC0415 - 延迟导入：没有 pyyaml 的环境照常工作
    except ImportError:
        return None
    try:
        return yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - 解析失败就是「不是结构化数据」
        return None


def _has_nested(tree) -> bool:
    """树里是否有嵌套（dict 的某个值 / list 的某个元素本身又是容器）。"""
    values = tree.values() if isinstance(tree, dict) else tree
    return any(isinstance(item, (dict, list)) for item in values)


def is_structured(text: str) -> bool:
    """文本是否是一棵「大或嵌套」的结构化树。"""
    tree = _yaml_tree(text)
    if not isinstance(tree, (dict, list)):
        return False
    if len(tree) >= STRUCTURED_MIN_KEYS:
        return True
    return len(tree) >= STRUCTURED_NESTED_MIN_KEYS and _has_nested(tree)


def skip_reason(entry) -> str:
    """判断一个世界书条目是否该**跳过**（不进 LLM 批次）。

    Args:
        entry: 世界书条目对象（`comment`/`name`/`content`）。

    Returns:
        跳过原因（`protected:name` / `protected:structured` / `protected:ini` /
        `protected:<标记>`），空串表示可以送。
    """
    if not isinstance(entry, dict):
        return ''

    name = _text(entry.get('comment') or entry.get('name'))
    content = _text(entry.get('content'))

    # 1. 名字信号
    for pattern in STRICT_NAME:
        if pattern.search(name):
            return 'protected:name'

    # 2. 结构化树
    if is_structured(content):
        return 'protected:structured'

    # 3. INI / 状态栏段
    if len(INI_SECTION.findall(content)) >= 3:
        return 'protected:ini'

    # 4. 旧标记与代码构造
    for kind, pattern in PROTECT_PATTERNS:
        if pattern.search(content):
            return f'protected:{kind}'

    return ''


#: 给用户看的解释（面板/回执里显示原因，而不是只给一个代码）
REASON_LABELS = {
    'protected:name': '名字命中数据/机制类（initvar、[核心]、[MVU] 等）',
    'protected:structured': '正文是结构化数据（YAML/JSON 树）',
    'protected:ini': '正文含 3 段以上 INI/状态栏模板',
    'protected:macro': '正文含双花括号宏',
    'protected:ejs': '正文含 EJS 模板',
    'protected:script': '正文含 <script> 块',
    'protected:html': '正文含布局 HTML',
    'protected:stscript': '正文含 STscript 指令',
    'protected:var_func': '正文含变量读写宏',
    'protected:js_code': '正文含 JS 代码特征',
    'protected:code_py': '正文含 Python 代码',
    'protected:code_js': '正文含 JS 代码',
    'protected:json_block': '正文含 JSON 块',
}


def describe(reason: str) -> str:
    """把跳过原因翻成人话（未知原因原样返回）。"""
    return REASON_LABELS.get(str(reason or ''), str(reason or ''))
