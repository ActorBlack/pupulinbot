"""Shared grouped rank, rating and point-progress header for player cards."""

from PIL import ImageDraw

from .typography import profile_face as face

INK, MUTED = "#24334a", "#7a879a"


def draw_profile_header(
    image,
    *,
    box,
    name,
    subtitle,
    current_rank,
    highest_rank,
    current_points,
    highest_points,
    progress,
    current_color=INK,
    highest_color="#75618c",
    progress_color="#d5a243",
    show_rating=False,
    rating="—",
    highest_rating="—",
    tag="",
    icon=None,
    rank_data=None,
    highest_rank_data=None,
    rank_mode=4,
):
    draw = ImageDraw.Draw(image)

    def text(x, y, value, size=24, color=INK, style="regular", width=None, anchor="lm"):
        value = str(value)
        if width:
            while size > 18 and draw.textlength(value, font=face(size, style)) > width:
                size -= 1
            while value and draw.textlength(value, font=face(size, style)) > width:
                value = value[:-2] + "…" if len(value) > 1 else ""
        draw.text((x, y), value, font=face(size, style), fill=color, anchor=anchor)

    name_x = 194 if icon else 82
    text(name_x, 95, name, 46, style="title", width=500 if icon else 620)
    text(name_x, 143, subtitle, 22, MUTED, width=500 if icon else 620)
    if icon:
        image.paste(icon, (78, 67), icon)
    if tag:
        label = tag
        while label and draw.textlength(label, font=face(19)) > 500:
            label = label[:-2] + "…" if len(label) > 1 else ""
        width = draw.textlength(label, font=face(19)) + 24
        draw.rounded_rectangle(
            (name_x - 10, 181, name_x - 10 + width, 214), radius=12, fill="#f1f3f8"
        )
        text(name_x + 2, 198, label, 19, "#788399")

    draw.line((734, 65, 734, box[3] - 28), fill="#e9eef5", width=2)
    rank_y = 78 if show_rating else 93
    point_y = 161 if show_rating else 157

    def paired_row(y, labels, values, size, style="number", colors=(INK, INK)):
        values = tuple(str(v or "—") for v in values)
        # Fit the pair together so neither side ends up with a smaller type size.
        while size > 18 and any(draw.textlength(v, font=face(size, style)) > 250 for v in values):
            size -= 1
        for x, label, val, color in zip((782, 1166), labels, values, colors, strict=True):
            text(x, y, label, 20, MUTED)
            text(x + 90, y, val, size, color, style, width=250)

    paired_row(
        rank_y,
        ("当前", "最高"),
        (current_rank, highest_rank),
        32,
        "bold",
        (current_color, highest_color),
    )
    if show_rating:
        paired_row(121, ("当前R", "最高R"), (rating, highest_rating), 28)
    if rank_data is not None or highest_rank_data is not None:
        from .ms_level import SOUL_COLOR, point_unit
        from .rank_assets import point_run, rank_badge

        for x, label, data in ((782, "当前", rank_data), (1166, "最高", highest_rank_data)):
            text(x, point_y, label + point_unit(data), 20, MUTED)
            run = (
                point_run(data, 23, capacity=True) if data and data["points"] is not None else None
            )
            if run is None:
                text(x + 90, point_y, "—", 25, MUTED)
            else:
                run.thumbnail((245, 30))
                image.paste(run, (x + 90, round(point_y - run.height / 2)), run)
        if highest_rank_data:
            badge = rank_badge(highest_rank_data["major"], highest_rank_data["star"], rank_mode, 48)
            if badge is not None:
                image.paste(badge, (1484, 40), badge)
        if rank_data and rank_data["major"] == 7:
            progress_color = SOUL_COLOR
    else:
        paired_row(point_y, ("当前PT", "最高PT"), (current_points, highest_points), 25)
    bar_y = 186 if show_rating else 197
    draw.rounded_rectangle((782, bar_y, 1518, bar_y + 7), radius=3, fill="#e9eef5")
    if progress is not None and progress > 0:
        draw.rounded_rectangle(
            (782, bar_y, 782 + max(7, 736 * min(1, progress)), bar_y + 7),
            radius=3,
            fill=progress_color,
        )
