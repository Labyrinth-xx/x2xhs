from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = [
    # macOS
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    # Ubuntu/Debian: apt install fonts-noto-cjk
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
]


def _resolve_font_path() -> str:
    import os
    # 优先读环境变量，方便 VPS 自定义
    env_path = os.getenv("FONT_PATH", "").strip()
    if env_path and Path(env_path).is_file():
        return env_path
    for p in _FONT_CANDIDATES:
        if Path(p).is_file():
            return p
    raise RuntimeError(
        "找不到中文字体，请安装后重试。\n"
        "Ubuntu/Debian: sudo apt install fonts-noto-cjk\n"
        "或在 .env 中设置: FONT_PATH=/path/to/your/font.ttc"
    )
LABEL_FONT_SIZE = 20
BODY_FONT_SIZE = 36
PAD_H = 48       # 水平内边距
PAD_TOP = 4      # 卡片顶部内边距
PAD_BOTTOM = 40  # 卡片底部内边距
LABEL_GAP = 16   # 标注与正文间距
LINE_SPACING = 10

# 扫描正文底边时，中央水平采样范围（占图片宽度的比例）
TEXT_SCAN_X_RATIO = (0.1, 0.9)
# 连续超过此行纯白/无文字则认为正文已结束
MAX_BLANK_ROWS = 60
# 引用卡片内文字→图片跳变：亮区→暗区的亮度阈值
QUOTE_TEXT_BRIGHT_MIN = 220
QUOTE_IMAGE_DARK_MAX = 150
# 引用卡片内文字区最小高度（防止误把紧邻图片误判为引用卡片）
QUOTE_MIN_TEXT_HEIGHT = 80

logger = logging.getLogger(__name__)


class TweetImageOverlayer:
    def __init__(self) -> None:
        self._font_path = _resolve_font_path()

    def append_translation(self, screenshot_path: Path, translation: str) -> Path | None:
        """在推文正文下方插入翻译卡片（正文后、图片前），返回新图片路径（*_zh.png）。"""
        return self.append_translations(screenshot_path, [translation])

    def append_translations(self, screenshot_path: Path, translations: list[str]) -> Path | None:
        """插入一个或多个翻译卡片，自动检测插入位置，返回新图片路径（*_zh.png）。

        translations[0] → 主推文正文后
        translations[1] → 引用推文正文后（如果存在引用卡片且有图片）
        """
        if not screenshot_path.exists():
            logger.warning("截图不存在，跳过翻译卡片生成: %s", screenshot_path)
            return None
        texts = [t.strip() for t in translations if t.strip()]
        if not texts:
            logger.warning("译文为空，跳过翻译卡片生成")
            return None

        try:
            output_path = screenshot_path.with_name(f"{screenshot_path.stem}_zh.png")
            self._render(screenshot_path, output_path, texts)
            logger.info("翻译卡片生成完成: %s", output_path.name)
            return output_path
        except Exception as exc:
            logger.warning("翻译卡片生成失败 [%s]: %s", screenshot_path, exc)
            return None

    def _render(self, screenshot_path: Path, output_path: Path, translations: list[str]) -> None:
        with Image.open(screenshot_path) as src:
            screenshot = src.convert("RGB")

        is_dark = self._is_dark(screenshot)
        label_font = ImageFont.truetype(self._font_path, LABEL_FONT_SIZE)
        body_font = ImageFont.truetype(self._font_path, BODY_FONT_SIZE)
        available_width = screenshot.width - PAD_H * 2

        # 检测所有插入点（最多与 translations 数量相同）
        insertion_points = self._find_all_insertion_points(screenshot)
        pairs = list(zip(translations, insertion_points))

        if not pairs:
            logger.debug("未检测到插入点，跳过")
            screenshot.close()
            return

        # 提前检测每个插入点的边框信息（用于引用卡片边框连续性）
        all_borders = [
            self._detect_quote_card_borders(screenshot, insert_y)
            for _, insert_y in pairs
        ]

        # 每个翻译卡片的文字范围：扫描对应段落的实际文字边界
        # 第一段：主推文（跳过顶部头像/账号行约 120px）
        # 后续段：引用卡片内文字（跳过引用卡片头部约 50px）
        SECTION_HEADER_SKIP = [120] + [50] * (len(pairs) - 1)
        text_bounds_list = []
        for i, (_, insert_y) in enumerate(pairs):
            prev_y = pairs[i - 1][1] if i > 0 else 0
            y_start = prev_y + SECTION_HEADER_SKIP[i]
            y_end = insert_y - 10
            bounds = self._detect_text_bounds(screenshot, y_start, y_end)
            text_bounds_list.append(bounds)

        # 构建每张卡片（文字范围与对应段落的英文正文对齐）
        cards = []
        for (text, _), bounds in zip(pairs, text_bounds_list):
            text_left, text_right = bounds if bounds else (PAD_H, screenshot.width - PAD_H)
            cards.append(
                self._make_card(
                    screenshot.width, text, body_font, label_font, is_dark,
                    text_left=text_left, text_right=text_right,
                )
            )

        # 从下往上插入，保持 Y 坐标不漂移
        result = screenshot
        for (_, insert_y), card, borders in reversed(list(zip(pairs, cards, all_borders))):
            logger.debug("翻译卡片插入位置 y=%d（当前图片高度=%d）", insert_y, result.height)
            if borders:
                card = self._apply_border_continuity(card, *borders)
            top = result.crop((0, 0, result.width, insert_y))
            bottom = result.crop((0, insert_y, result.width, result.height))
            combined = Image.new("RGB", (result.width, result.height + card.height))
            combined.paste(top, (0, 0))
            combined.paste(card, (0, insert_y))
            combined.paste(bottom, (0, insert_y + card.height))
            top.close()
            bottom.close()
            if result is not screenshot:
                result.close()
            result = combined

        result.save(output_path)
        result.close()
        screenshot.close()
        for card in cards:
            card.close()

    def _make_card(
        self,
        width: int,
        translation: str,
        body_font: ImageFont.FreeTypeFont,
        label_font: ImageFont.FreeTypeFont,
        is_dark: bool,
        text_left: int = PAD_H,
        text_right: int | None = None,
    ) -> Image.Image:
        if text_right is None:
            text_right = width - PAD_H
        inner_width = text_right - text_left

        bg_color = (30, 30, 30) if is_dark else (255, 255, 255)
        label_color = (160, 160, 160)
        text_color = (236, 236, 236) if is_dark else (15, 15, 15)

        lines = self._wrap(translation, body_font, inner_width)
        label_h = LABEL_FONT_SIZE
        PARA_SPACING = int(BODY_FONT_SIZE * 0.8)  # 段落空行高度
        body_h = sum(
            PARA_SPACING if not line else (BODY_FONT_SIZE + LINE_SPACING)
            for line in lines
        ) - LINE_SPACING
        card_h = PAD_TOP + label_h + LABEL_GAP + body_h + PAD_BOTTOM

        card = Image.new("RGB", (width, card_h), bg_color)
        draw = ImageDraw.Draw(card)
        draw.text((text_left, PAD_TOP), "由 Claude 翻译自英语", fill=label_color, font=label_font)
        y = PAD_TOP + label_h + LABEL_GAP
        for line in lines:
            if not line:
                y += PARA_SPACING
            else:
                draw.text((text_left, y), line, fill=text_color, font=body_font)
                y += BODY_FONT_SIZE + LINE_SPACING

        return card

    def _detect_text_bounds(
        self, image: Image.Image, y_start: int, y_end: int
    ) -> tuple[int, int] | None:
        """扫描 [y_start, y_end] 行区间，找到最左和最右的深色文字像素 x 坐标。"""
        DARK_THRESH = 80
        pixels = image.load()
        width = image.width
        min_x = width
        max_x = 0

        for y in range(max(0, y_start), min(image.height, y_end), 2):
            for x in range(10, width // 2):
                r, g, b = pixels[x, y]
                if (r + g + b) / 3 < DARK_THRESH:
                    if x < min_x:
                        min_x = x
                    break
            for x in range(width - 10, width // 2, -1):
                r, g, b = pixels[x, y]
                if (r + g + b) / 3 < DARK_THRESH:
                    if x > max_x:
                        max_x = x
                    break

        if min_x >= max_x:
            return None
        return (min_x, max_x)

    def _detect_quote_card_borders(
        self, image: Image.Image, insert_y: int
    ) -> tuple[int, int, tuple[int, int, int], tuple[int, int, int]] | None:
        """检测插入点处是否处于引用卡片内，并返回边框信息。

        返回 (left_x, right_x, border_color, outer_bg) 或 None（非引用卡片区域）。
        left_x/right_x：边框最外侧像素的 x 坐标。
        """
        width = image.width
        # 在插入点上方采样，避免恰好踩在分隔线上
        sample_y = max(0, insert_y - 20)

        # 从左侧找第一个非白像素
        left_x: int | None = None
        for x in range(0, width // 2):
            p = image.getpixel((x, sample_y))
            if (p[0] + p[1] + p[2]) / 3 < 235:
                left_x = x
                break

        # 从右侧找第一个非白像素
        right_x: int | None = None
        for x in range(width - 1, width // 2, -1):
            p = image.getpixel((x, sample_y))
            if (p[0] + p[1] + p[2]) / 3 < 235:
                right_x = x
                break

        if left_x is None or right_x is None:
            return None
        # 边框间距必须足够宽（>50% 图宽），否则误判
        if (right_x - left_x) < width * 0.5:
            return None

        border_color: tuple[int, int, int] = image.getpixel((left_x, sample_y))
        outer_bg: tuple[int, int, int] = image.getpixel((0, sample_y))
        logger.debug("检测到引用卡片边框: left_x=%d right_x=%d color=%s", left_x, right_x, border_color)
        return left_x, right_x, border_color, outer_bg

    def _apply_border_continuity(
        self,
        card: Image.Image,
        left_x: int,
        right_x: int,
        border_color: tuple[int, int, int],
        outer_bg: tuple[int, int, int],
    ) -> Image.Image:
        """在翻译卡片上重建引用卡片的左右边框，外侧填充外部背景色。"""
        draw = ImageDraw.Draw(card)
        # 外侧区域填充外部背景（隐藏卡片超出引用框的部分）
        if left_x > 0:
            draw.rectangle([(0, 0), (left_x - 1, card.height - 1)], fill=outer_bg)
        if right_x < card.width - 1:
            draw.rectangle([(right_x + 1, 0), (card.width - 1, card.height - 1)], fill=outer_bg)
        # 重绘边框线（宽度 2px，覆盖原始边框宽度）
        draw.line([(left_x, 0), (left_x, card.height - 1)], fill=border_color, width=2)
        draw.line([(right_x, 0), (right_x, card.height - 1)], fill=border_color, width=2)
        return card

    def _find_all_insertion_points(self, image: Image.Image) -> list[int]:
        """返回所有翻译卡片的插入 Y 坐标列表。

        通常返回 1 个（普通推文）或 2 个（含引用卡片）。
        引用卡片可以含图片（亮→暗跳变）或纯文字（扫文字底边）。
        """
        y1 = self._find_text_bottom(image)
        points = [y1]

        y2 = self._find_quoted_image_start(image, start_y=y1)
        if y2 is None:
            y2 = self._find_quoted_text_bottom(image, start_y=y1)
        if y2 is not None:
            points.append(y2)

        return points

    def _find_quoted_image_start(self, image: Image.Image, start_y: int) -> int | None:
        """在引用卡片内，找到引用推文正文结束、图片开始的位置。

        特征：start_y 之后出现 亮区（avg>220）→ 暗区（avg<150）的跳变，
        且跳变距离 start_y 至少 QUOTE_MIN_TEXT_HEIGHT px（确保中间有文字区）。
        """
        x1 = int(image.width * 0.05)
        x2 = int(image.width * 0.95)
        sample_xs = list(range(x1, x2, 8))
        if not sample_xs:
            return None

        scan_limit = min(image.height - 100, start_y + 600)
        prev_avg: float | None = None

        for y in range(start_y + 20, scan_limit):
            pixels = [image.getpixel((x, y)) for x in sample_xs]
            gray_vals = [(p[0] + p[1] + p[2]) / 3 for p in pixels]
            avg = sum(gray_vals) / len(gray_vals)

            if (
                prev_avg is not None
                and prev_avg > QUOTE_TEXT_BRIGHT_MIN
                and avg < QUOTE_IMAGE_DARK_MAX
                and (y - start_y) > QUOTE_MIN_TEXT_HEIGHT
            ):
                logger.debug("检测到引用卡片图片起始 y=%d（prev_avg=%.1f → avg=%.1f）", y, prev_avg, avg)
                return y - 2

            prev_avg = avg

        return None

    def _find_text_bottom(self, image: Image.Image) -> int:
        """定位正文底边，作为翻译卡片插入点。

        合并扫描：在追踪文字行的同时检测灰色分隔线。
        灰线必须出现在已见到文字之后才接受；
        灰线后若又出现文字则自动重置（说明是嵌入元素的边框，不是正文结束）。
        """
        width = image.width
        x1 = int(width * TEXT_SCAN_X_RATIO[0])
        x2 = int(width * TEXT_SCAN_X_RATIO[1])
        sample_xs = list(range(x1, x2, 8))
        if not sample_xs:
            return image.height

        gray = image.convert("L")
        last_text_y = 0
        blank_count = 0
        found_text = False
        separator_y: int | None = None

        for y in range(100, image.height):
            gray_pixels = [gray.getpixel((x, y)) for x in sample_xs]
            dark = [p for p in gray_pixels if p < 100]
            has_text = len(dark) >= 2

            # 灰色分隔线检测：只在已见到文字且尚无候选分隔线时检测
            if found_text and separator_y is None:
                rgb_pixels = [image.getpixel((x, y)) for x in sample_xs]
                rgb_vals = [(p[0] + p[1] + p[2]) / 3 for p in rgb_pixels]
                avg = sum(rgb_vals) / len(rgb_vals)
                variance = sum((v - avg) ** 2 for v in rgb_vals) / len(rgb_vals)
                if variance < 30 and 180 < avg < 242:
                    separator_y = y

            if has_text:
                last_text_y = y
                blank_count = 0
                found_text = True
                separator_y = None  # 分隔线后又出现文字 → 是嵌入元素边框，重置
            elif found_text:
                blank_count += 1
                if blank_count >= MAX_BLANK_ROWS:
                    break

        if separator_y is not None:
            logger.debug("找到分隔线 y=%d", separator_y)
            return separator_y

        if not found_text:
            logger.debug("未找到文字，翻译卡片放到底部")
            return image.height

        return min(image.height, last_text_y + 20)

    def _find_quoted_text_bottom(self, image: Image.Image, start_y: int) -> int | None:
        """引用卡片（纯文字，无嵌入图片）的文字底边检测。

        在 start_y 之后跳过引用卡片头部（头像+账号行约 80px），
        找到引用正文的最后一行文字。找不到则返回 None。
        """
        gray = image.convert("L")
        width = image.width
        x1 = int(width * TEXT_SCAN_X_RATIO[0])
        x2 = int(width * TEXT_SCAN_X_RATIO[1])
        sample_xs = list(range(x1, x2, 8))

        QUOTE_HEADER_SKIP = 80
        scan_start = start_y + QUOTE_HEADER_SKIP
        scan_end = min(image.height - 50, start_y + 600)

        if scan_start >= scan_end:
            return None

        last_text_y = 0
        blank_count = 0
        found_text = False

        for y in range(scan_start, scan_end):
            pixels = [gray.getpixel((x, y)) for x in sample_xs]
            dark = [p for p in pixels if p < 100]
            has_text = len(dark) >= 2

            if has_text:
                last_text_y = y
                blank_count = 0
                found_text = True
            elif found_text:
                blank_count += 1
                if blank_count >= MAX_BLANK_ROWS:
                    break

        if not found_text or last_text_y <= scan_start + 10:
            return None

        logger.debug("检测到引用推文文字底边 y=%d", last_text_y)
        return min(image.height, last_text_y + 20)

    def _is_dark(self, image: Image.Image) -> bool:
        """采样图片左上角判断是否为暗色主题。"""
        sample = image.crop((0, 0, min(20, image.width), min(20, image.height)))
        pixels = list(sample.convert("RGB").getdata())
        if not pixels:
            return False
        avg = sum(sum(p) / 3 for p in pixels) / len(pixels)
        return avg < 128

    def _wrap(self, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
        """按换行符拆段，每段词级自动换行，遵循中文排版禁则。

        规则：
        - 英文单词/数字串作为整体 token，不在中间截断
        - 行首禁则：句末标点、右括号、间隔号、% ° 等不出现在行首
        - 行尾禁则：左括号类不出现在行尾（移到下一行开头）
        - 破折号/省略号（——/……）作为整体 token 不拆分
        """
        import re as _re

        # 行首禁则：这些字符不能出现在行首
        NO_LINE_START = set("。，、；：！？）】」』〕〗〉》·…—%°")
        # 行尾禁则：这些字符不能出现在行尾
        NO_LINE_END = set("（【「『〔〖〈《")

        result: list[str] = []
        dummy = ImageDraw.Draw(Image.new("RGB", (1, 1)))

        for segment in text.split("\n"):
            if not segment.strip():
                result.append("")
                continue

            # token 规则：
            #   1. 英文单词/数字串（含常见符号）整体作为一个 token
            #   2. ——  …… 作为整体
            #   3. 空白序列
            #   4. 其余单字符
            tokens = _re.findall(
                r"[A-Za-z0-9+\-_./#%]+|——|……|\s+|[^\s]", segment
            )

            current = ""
            for token in tokens:
                # 行首跳过空白
                if not current and not token.strip():
                    continue

                trial = current + token
                if dummy.textlength(trial, font=font) <= max_width:
                    current = trial
                else:
                    if not current:
                        # 单 token 超宽，强制放入（避免死循环）
                        current = token
                        continue

                    # 行首禁则：token 首字符不可在行首，附到当前行末
                    if token and token[0] in NO_LINE_START:
                        current += token[0]
                        result.append(current.rstrip())
                        current = token[1:].lstrip() if len(token) > 1 else ""
                        continue

                    # 行尾禁则：当前行末字符不可在行尾，移到下一行
                    stripped = current.rstrip()
                    if stripped and stripped[-1] in NO_LINE_END:
                        carry = stripped[-1]
                        result.append(stripped[:-1].rstrip())
                        current = carry + token
                    else:
                        result.append(stripped)
                        current = token.lstrip()

            if current.strip():
                result.append(current.rstrip())

        return result or [""]
