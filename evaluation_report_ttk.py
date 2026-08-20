"""Read-only TTK viewer for evaluator Excel reports."""

from __future__ import annotations

import argparse
import json
import sys
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from openpyxl import load_workbook
from PIL import Image, ImageTk


DETAIL_SHEET = "评测明细"
REQUIRED_COLUMNS = ("源行号", "任务指令", "图片ID", "期望结果", "系统标准动作")
RED = "#ff3b30"
BLUE = "#1687ff"


@dataclass(frozen=True)
class ReportCase:
    report_row: int
    source_row: int
    instruction: str
    image_id: str
    expected_raw: str
    model_raw: str
    selected_engine: str
    correct: bool
    comparison: str
    error: str


def cell_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_action(raw: str) -> dict[str, Any] | None:
    if not raw.strip():
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("动作必须是 JSON 对象。")
    return value


def action_name(action: dict[str, Any] | None) -> str:
    if action is None:
        return "无"
    return str(action.get("action") or action.get("action_id") or "未知")


def engine_actions(raw: str) -> list[tuple[str, dict[str, Any]]]:
    value = parse_action(raw)
    if value is None:
        return []
    if "action" in value or "action_id" in value:
        return [("模型", value)]
    actions = []
    for engine, action in value.items():
        if isinstance(action, dict) and ("action" in action or "action_id" in action):
            actions.append((str(engine), action))
    return actions


def compact_action(raw: str) -> str:
    try:
        value = parse_action(raw)
        return "" if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (ValueError, json.JSONDecodeError):
        return raw


def action_summary(action: dict[str, Any]) -> str:
    return json.dumps(action, ensure_ascii=False, separators=(",", ":"))


def resolve_report_image(image_id: str, report_dir: Path) -> Path:
    value = Path(image_id).expanduser()
    candidate = value if value.is_absolute() else report_dir / "device_captures" / value
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"未找到图片：{candidate}")
    return candidate


def load_report(path: Path) -> list[ReportCase]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if DETAIL_SHEET not in workbook.sheetnames:
            raise ValueError(f"评测报告中没有 {DETAIL_SHEET!r} 工作表。")
        sheet = workbook[DETAIL_SHEET]
        headers = {cell_text(cell.value): cell.column for cell in sheet[1]}
        missing = [column for column in REQUIRED_COLUMNS if column not in headers]
        if missing:
            raise ValueError("评测明细缺少列：" + "、".join(missing))

        cases: list[ReportCase] = []
        for row in range(2, sheet.max_row + 1):
            instruction = cell_text(sheet.cell(row, headers["任务指令"]).value)
            image_id = cell_text(sheet.cell(row, headers["图片ID"]).value)
            if not instruction and not image_id:
                continue
            pixel_column = headers.get("系统像素动作")
            standard_column = headers["系统标准动作"]
            model_raw = cell_text(
                sheet.cell(row, pixel_column or standard_column).value
            )
            if not model_raw and pixel_column:
                model_raw = cell_text(sheet.cell(row, standard_column).value)
            correct_value = sheet.cell(row, headers.get("是否正确", 0)).value if "是否正确" in headers else False
            source_value = sheet.cell(row, headers["源行号"]).value
            cases.append(
                ReportCase(
                    row,
                    int(source_value) if isinstance(source_value, (int, float)) else row,
                    instruction,
                    image_id,
                    cell_text(sheet.cell(row, headers["期望结果"]).value),
                    model_raw,
                    cell_text(sheet.cell(row, headers.get("执行主体", 0)).value) if "执行主体" in headers else "",
                    bool(correct_value),
                    cell_text(sheet.cell(row, headers.get("判分说明", 0)).value) if "判分说明" in headers else "",
                    cell_text(sheet.cell(row, headers.get("错误", 0)).value) if "错误" in headers else "",
                )
            )
        if not cases:
            raise ValueError("评测明细中没有可查看的用例。")
        return cases
    finally:
        workbook.close()


def swipe_end(action: dict[str, Any], width: int, height: int) -> tuple[float, float] | None:
    end = action.get("end_coordinate")
    if isinstance(end, list) and len(end) == 2:
        return float(end[0]), float(end[1])
    start = action.get("start_coordinate")
    direction = action.get("direction")
    if not isinstance(start, list) or len(start) != 2 or direction not in {"up", "down", "left", "right"}:
        return None
    fraction = {"short": 0.15, "medium": 0.3, "long": 0.55}.get(action.get("distance"), 0.3)
    x, y = float(start[0]), float(start[1])
    if direction == "left":
        x -= width * fraction
    elif direction == "right":
        x += width * fraction
    elif direction == "up":
        y -= height * fraction
    else:
        y += height * fraction
    return max(0, min(width - 1, x)), max(0, min(height - 1, y))


class OverlayCanvas(tk.Canvas):
    def __init__(self, master):
        super().__init__(master, bg="#202020", highlightthickness=0)
        self.image: Image.Image | None = None
        self.photo = None
        self.expected: dict[str, Any] | None = None
        self.model: list[tuple[str, dict[str, Any]]] = []
        self.scale = 1.0
        self.offset_x = self.offset_y = 0
        self.bind("<Configure>", lambda _event: self.redraw())

    def configure_case(self, image, expected, model):
        self.image = image
        self.expected = expected
        self.model = model
        self.redraw()

    def canvas_point(self, point):
        return (
            self.offset_x + float(point[0]) * self.scale,
            self.offset_y + float(point[1]) * self.scale,
        )

    def draw_action(self, action, color, label):
        if not action or self.image is None:
            return
        name = action_name(action)
        if name == "click":
            boxes = action.get("bbox")
            if isinstance(boxes, list) and len(boxes) == 4 and all(
                isinstance(value, (int, float)) for value in boxes
            ):
                boxes = [boxes]
            if isinstance(boxes, list):
                for box in boxes:
                    if isinstance(box, list) and len(box) == 4:
                        start = self.canvas_point(box[:2])
                        end = self.canvas_point(box[2:])
                        self.create_rectangle(*start, *end, outline=color, width=3)
                        self.create_text(start[0] + 4, start[1] + 4, text=label, fill=color, anchor="nw", font=("TkDefaultFont", 10, "bold"))
            coordinate = action.get("coordinate")
            if coordinate is None and all(key in action for key in ("x", "y")):
                coordinate = [action["x"], action["y"]]
            if isinstance(coordinate, list) and len(coordinate) == 2:
                x, y = self.canvas_point(coordinate)
                radius = 8
                self.create_oval(x-radius, y-radius, x+radius, y+radius, outline=color, width=3)
                self.create_line(x-radius-4, y, x+radius+4, y, fill=color, width=2)
                self.create_line(x, y-radius-4, x, y+radius+4, fill=color, width=2)
                self.create_text(x + 11, y + 11, text=label, fill=color, anchor="nw", font=("TkDefaultFont", 10, "bold"))
        elif name == "swipe":
            start = action.get("start_coordinate")
            end = swipe_end(action, self.image.width, self.image.height)
            if isinstance(start, list) and len(start) == 2 and end is not None:
                a, z = self.canvas_point(start), self.canvas_point(end)
                self.create_line(*a, *z, fill=color, width=4, arrow=tk.LAST, arrowshape=(14, 18, 7))
                self.create_text(a[0] + 4, a[1] + 4, text=label, fill=color, anchor="nw", font=("TkDefaultFont", 10, "bold"))

    def draw_action_labels(self, width: int, height: int):
        entries = []
        if self.expected:
            entries.append((RED, "标准", self.expected))
        entries.extend((BLUE, f"模型-{engine}", action) for engine, action in self.model)
        if not entries:
            return
        line_height = 24
        panel_height = min(height - 12, 12 + line_height * len(entries))
        top = height - panel_height
        self.create_rectangle(8, top, width - 8, height - 8, fill="#151515", outline="#555555", width=1)
        for index, (color, label, action) in enumerate(entries):
            y = top + 8 + index * line_height
            text = f"{label}: {action_summary(action)}"
            self.create_text(
                16, y, text=text, fill=color, anchor="nw",
                width=max(100, width - 32), font=("TkDefaultFont", 10, "bold"),
            )

    def redraw(self):
        self.delete("all")
        width, height = max(1, self.winfo_width()), max(1, self.winfo_height())
        if self.image is None:
            self.create_text(width / 2, height / 2, text="无法显示图片", fill="white")
            return
        self.scale = min(width / self.image.width, height / self.image.height)
        shown_size = (
            max(1, round(self.image.width * self.scale)),
            max(1, round(self.image.height * self.scale)),
        )
        shown = self.image.resize(shown_size, Image.Resampling.LANCZOS)
        self.offset_x = (width - shown_size[0]) // 2
        self.offset_y = (height - shown_size[1]) // 2
        self.photo = ImageTk.PhotoImage(shown)
        self.create_image(self.offset_x, self.offset_y, image=self.photo, anchor="nw")
        self.draw_action(self.expected, RED, "标准")
        for engine, action in self.model:
            self.draw_action(action, BLUE, f"模型-{engine}")
        self.create_rectangle(10, 10, 245, 42, fill="#202020", outline="")
        self.create_text(18, 26, text="■ 标准结果    ■ 模型结果", fill="white", anchor="w")
        self.create_rectangle(18, 19, 29, 30, fill=RED, outline=RED)
        self.create_rectangle(113, 19, 124, 30, fill=BLUE, outline=BLUE)
        self.draw_action_labels(width, height)


class ReportViewer:
    def __init__(self, root: tk.Tk, report_path: Path):
        self.root = root
        self.report_path = report_path.resolve()
        self.cases = load_report(self.report_path)
        self.index = 0
        self.selecting = False
        self.progress = tk.StringVar()
        self.status = tk.StringVar()
        self.instruction = tk.StringVar()
        self.expected_text = tk.StringVar()
        self.model_text = tk.StringVar()
        self.meta_text = tk.StringVar()
        root.title(f"评测结果只读检查（红=标准，蓝=模型）- {self.report_path.name}")
        root.geometry("1600x920")
        root.minsize(1100, 700)
        self.build()
        root.bind("<Left>", lambda _event: self.navigate(-1))
        root.bind("<Right>", lambda _event: self.navigate(1))
        self.show_case(0)

    def build(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(self.root, padding=8)
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Button(toolbar, text="上一条", command=lambda: self.navigate(-1)).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="下一条", command=lambda: self.navigate(1)).pack(side=tk.LEFT, padx=6)
        ttk.Label(toolbar, textvariable=self.progress).pack(side=tk.LEFT, padx=12)
        ttk.Label(toolbar, text="红色：标准结果", foreground=RED).pack(side=tk.RIGHT, padx=8)
        ttk.Label(toolbar, text="蓝色：模型结果", foreground=BLUE).pack(side=tk.RIGHT)

        panes = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        panes.grid(row=1, column=0, sticky="nsew", padx=8)
        left = ttk.Frame(panes)
        right = ttk.Frame(panes, padding=(8, 0, 0, 0))
        panes.add(left, weight=3)
        panes.add(right, weight=2)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.canvas = OverlayCanvas(left)
        self.canvas.grid(row=0, column=0, sticky="nsew")

        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=3)
        right.rowconfigure(1, weight=2)
        columns = ("row", "instruction", "expected", "model", "result")
        self.case_list = ttk.Treeview(right, columns=columns, show="headings", selectmode="browse")
        headings = {"row": "行", "instruction": "单步指令", "expected": "标准结果", "model": "模型结果", "result": "判定"}
        widths = {"row": 48, "instruction": 220, "expected": 190, "model": 190, "result": 58}
        for column in columns:
            self.case_list.heading(column, text=headings[column])
            self.case_list.column(column, width=widths[column], anchor="w")
        scrollbar = ttk.Scrollbar(right, orient=tk.VERTICAL, command=self.case_list.yview)
        self.case_list.configure(yscrollcommand=scrollbar.set)
        self.case_list.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        for index, case in enumerate(self.cases):
            result = "错误" if case.error else ("正确" if case.correct else "错误")
            self.case_list.insert("", tk.END, iid=str(index), values=(
                case.source_row,
                case.instruction,
                compact_action(case.expected_raw),
                compact_action(case.model_raw),
                result,
            ))
        self.case_list.tag_configure("incorrect", foreground="#c62828")
        self.case_list.bind("<<TreeviewSelect>>", self.on_select)

        detail = ttk.LabelFrame(right, text="当前用例详情", padding=8)
        detail.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        detail.columnconfigure(1, weight=1)
        fields = (
            ("单步指令", self.instruction),
            ("标准结果（红）", self.expected_text),
            ("模型结果（蓝）", self.model_text),
            ("判定信息", self.meta_text),
        )
        for row, (label, variable) in enumerate(fields):
            ttk.Label(detail, text=label + "：").grid(row=row, column=0, sticky="nw", pady=3)
            ttk.Label(detail, textvariable=variable, wraplength=560, justify=tk.LEFT).grid(row=row, column=1, sticky="nw", pady=3)
        ttk.Label(self.root, textvariable=self.status, anchor="w", relief="sunken", padding=(6, 3)).grid(row=2, column=0, sticky="ew", pady=(8, 0))

    def show_case(self, index: int):
        self.index = index
        case = self.cases[index]
        self.progress.set(f"{index + 1}/{len(self.cases)}  Excel 源行：{case.source_row}  图片：{case.image_id}")
        self.instruction.set(case.instruction)
        self.expected_text.set(compact_action(case.expected_raw) or "无")
        self.model_text.set(compact_action(case.model_raw) or "无")
        self.meta_text.set(
            f"{'ERROR' if case.error else ('PASS' if case.correct else 'FAIL')} | "
            f"engine={case.selected_engine or '-'} | {case.error or case.comparison}"
        )
        errors = []
        try:
            expected = parse_action(case.expected_raw)
        except (ValueError, json.JSONDecodeError) as error:
            expected = None
            errors.append(f"标准结果无法解析：{error}")
        try:
            model = engine_actions(case.model_raw)
        except (ValueError, json.JSONDecodeError) as error:
            model = []
            errors.append(f"模型结果无法解析：{error}")
        image = None
        try:
            with Image.open(resolve_report_image(case.image_id, self.report_path.parent)) as source:
                source.load()
                image = source.convert("RGB")
        except OSError as error:
            errors.append(str(error))
        self.canvas.configure_case(image, expected, model)
        self.status.set("；".join(errors) if errors else "已加载")
        self.selecting = True
        self.case_list.selection_set(str(index))
        self.case_list.focus(str(index))
        self.case_list.see(str(index))
        self.selecting = False

    def navigate(self, delta: int):
        target = self.index + delta
        if 0 <= target < len(self.cases):
            self.show_case(target)
        else:
            self.status.set("已经是第一条" if target < 0 else "已经是最后一条")

    def on_select(self, _event=None):
        if self.selecting:
            return
        selected = self.case_list.selection()
        if selected:
            self.show_case(int(selected[0]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="TTK evaluator report visual checker")
    parser.add_argument("report", nargs="?", type=Path, help="Evaluator result .xlsx")
    args = parser.parse_args(argv)
    print("[REPORT] 正在初始化只读评测查看器...", flush=True)
    root = tk.Tk()
    root.withdraw()
    selected = args.report or filedialog.askopenfilename(
        title="选择 evaluator 测试报告",
        filetypes=(("Excel 工作簿", "*.xlsx"), ("所有文件", "*.*")),
    )
    if not selected:
        root.destroy()
        return 0
    try:
        print(f"[REPORT] 正在加载评测报告：{Path(selected).resolve()}", flush=True)
        root.deiconify()
        ReportViewer(root, Path(selected))
        root.lift()
        root.after(100, root.focus_force)
        print("[REPORT] 只读界面加载完成。", flush=True)
    except Exception as error:
        print(f"[REPORT] 加载失败：{type(error).__name__}: {error}", file=sys.stderr, flush=True)
        messagebox.showerror("无法打开评测报告", str(error), parent=root)
        root.destroy()
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
