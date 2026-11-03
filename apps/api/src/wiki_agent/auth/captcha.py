from __future__ import annotations

import math
from dataclasses import dataclass

from pi_captcha import SlideBuilder
from pi_captcha.base.option import RangeVal, Size
from pi_captcha.slide.block import GraphImage
from PIL import Image, ImageChops, ImageDraw, ImageFilter


@dataclass(frozen=True)
class CaptchaChallenge:
    background_image: str
    puzzle_image: str
    target_x: int
    puzzle_width: int
    puzzle_height: int
    puzzle_top: int


class CaptchaGenerator:
    width = 320
    height = 160
    puzzle_size = 48

    def __init__(self) -> None:
        backgrounds = self._create_backgrounds()
        graph_image = self._create_graph_image()
        self._builder = SlideBuilder(
            SlideBuilder.with_image_size(Size(self.width, self.height)),
            SlideBuilder.with_range_graph_size(
                RangeVal(self.puzzle_size, self.puzzle_size)
            ),
        ).set_resources(
            SlideBuilder.with_backgrounds(backgrounds),
            SlideBuilder.with_graph_images([graph_image]),
        )

    def generate(self) -> CaptchaChallenge:
        data = self._builder.make().generate()
        block = data.get_data()
        return CaptchaChallenge(
            background_image=data.get_master_image().to_base64(quality=88),
            puzzle_image=data.get_tile_image().to_base64(),
            target_x=block.x,
            puzzle_width=block.width,
            puzzle_height=block.height,
            puzzle_top=block.y,
        )

    @classmethod
    def _create_backgrounds(cls) -> list[Image.Image]:
        palettes = (
            ("#153f43", "#78a99f", "#efbd79", "#d4eee8"),
            ("#243b62", "#87adc8", "#e7ae7d", "#d7e6f2"),
            ("#263f37", "#9bab83", "#e8c47f", "#dbe8d1"),
            ("#49374f", "#b18497", "#e1b56f", "#ead9e2"),
        )
        backgrounds: list[Image.Image] = []
        width, height = 720, 360
        for index, (start, end, accent, glow) in enumerate(palettes):
            image = cls._horizontal_gradient(width, height, start, end).convert("RGBA")

            glow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            glow_draw = ImageDraw.Draw(glow_layer)
            glow_draw.ellipse(
                (-180 + index * 35, -190, 310 + index * 25, 260),
                fill=(*cls._hex_rgb(glow), 145),
            )
            glow_draw.ellipse(
                (430 - index * 22, 120, 850, 510),
                fill=(*cls._hex_rgb(accent), 115),
            )
            glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(78))
            image = Image.alpha_composite(image, glow_layer)

            detail = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            draw = ImageDraw.Draw(detail)
            draw.rounded_rectangle(
                (42 + index * 16, 48, 286 + index * 18, 292),
                radius=38,
                fill=(255, 255, 255, 15),
                outline=(255, 255, 255, 42),
                width=2,
            )
            draw.rounded_rectangle(
                (470 - index * 12, 38 + index * 10, 685, 188 + index * 8),
                radius=30,
                fill=(255, 255, 255, 11),
                outline=(255, 255, 255, 30),
                width=2,
            )

            for wave in range(4):
                points = [
                    (
                        x,
                        int(
                            96
                            + wave * 46
                            + math.sin((x / 72) + index * 0.8 + wave) * 18
                        ),
                    )
                    for x in range(-20, width + 20, 10)
                ]
                draw.line(
                    points,
                    fill=(255, 255, 255, 26 + wave * 5),
                    width=2,
                    joint="curve",
                )

            node_positions = (
                (112 + index * 17, 112),
                (226, 236 - index * 8),
                (384 + index * 11, 96 + index * 18),
                (548, 244),
                (640 - index * 13, 116),
            )
            for node_index, (x, y) in enumerate(node_positions):
                if node_index:
                    previous_x, previous_y = node_positions[node_index - 1]
                    draw.line(
                        (previous_x, previous_y, x, y),
                        fill=(255, 255, 255, 35),
                        width=2,
                    )
                radius = 5 if node_index % 2 else 7
                draw.ellipse(
                    (x - radius, y - radius, x + radius, y + radius),
                    fill=(*cls._hex_rgb(accent), 175),
                    outline=(255, 255, 255, 145),
                    width=2,
                )
            image = Image.alpha_composite(image, detail)

            noise = Image.effect_noise((width, height), 12).convert("RGB")
            backgrounds.append(Image.blend(image.convert("RGB"), noise, 0.035))
        return backgrounds

    @classmethod
    def _create_graph_image(cls) -> GraphImage:
        size = 64
        scale = 4
        mask_large = Image.new("L", (size * scale, size * scale), 0)
        draw = ImageDraw.Draw(mask_large)

        def box(left: int, top: int, right: int, bottom: int) -> tuple[int, int, int, int]:
            return (
                left * scale,
                top * scale,
                right * scale,
                bottom * scale,
            )

        draw.rounded_rectangle(box(8, 8, 56, 56), radius=5 * scale, fill=255)
        draw.ellipse(box(23, -1, 41, 17), fill=255)
        draw.ellipse(box(47, 23, 65, 41), fill=255)
        draw.ellipse(box(23, 47, 41, 65), fill=0)
        mask_alpha = mask_large.resize(
            (size, size),
            Image.Resampling.LANCZOS,
        )

        shape = Image.new("RGBA", (size, size), (255, 255, 255, 0))
        shape.putalpha(mask_alpha)

        outside = mask_alpha.filter(ImageFilter.MaxFilter(5))
        inside = mask_alpha.filter(ImageFilter.MinFilter(5))
        outline_alpha = ImageChops.subtract(outside, inside)

        overlay = Image.new("RGBA", (size, size), (255, 255, 255, 0))
        overlay.putalpha(outline_alpha.point(lambda alpha: int(alpha * 0.82)))

        shadow = Image.new("RGBA", (size, size), (9, 27, 26, 0))
        shadow.putalpha(mask_alpha.point(lambda alpha: int(alpha * 0.42)))
        shadow_rim = Image.new("RGBA", (size, size), (255, 255, 255, 0))
        shadow_rim.putalpha(outline_alpha.point(lambda alpha: int(alpha * 0.3)))
        shadow = Image.alpha_composite(shadow, shadow_rim)

        graph = GraphImage()
        graph.mask_image = shape
        graph.overlay_image = overlay
        graph.shadow_image = shadow
        return graph

    @staticmethod
    def _hex_rgb(value: str) -> tuple[int, int, int]:
        normalized = value.lstrip("#")
        return (
            int(normalized[0:2], 16),
            int(normalized[2:4], 16),
            int(normalized[4:6], 16),
        )

    @classmethod
    def _horizontal_gradient(
        cls,
        width: int,
        height: int,
        start: str,
        end: str,
    ) -> Image.Image:
        start_rgb = cls._hex_rgb(start)
        end_rgb = cls._hex_rgb(end)
        image = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(image)
        for x in range(width):
            ratio = x / (width - 1)
            color = tuple(
                round(start_channel + (end_channel - start_channel) * ratio)
                for start_channel, end_channel in zip(start_rgb, end_rgb, strict=True)
            )
            draw.line((x, 0, x, height), fill=color)
        return image
