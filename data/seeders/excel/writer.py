# data/writer.py
# Excel 造数输出器（target: excel）：以官方导入模板为母版，按分批大小流式写出 xlsx
#
# 关键约定（与后端导入模板严格对齐）：
#   - 第 1 行表头、第 2 行后端写死的样例数据：均保留不动
#   - 生成的数据一律从【第 3 行】开始写入
#   - 母版中的下拉数据校验区域（默认覆盖到第 1001 行）按数据行数扩展
#   - 字段顺序与表头以母版为准，写入前做严格一致性校验
#
# 独立性约定：仅依赖 openpyxl + 标准库 logging，不 import core.*
import logging
import datetime as _dt
from pathlib import Path

import openpyxl
from openpyxl.utils import range_boundaries, get_column_letter

logger = logging.getLogger(__name__)

# openpyxl 保存 xlsx 的 mime 类型（multipart 上传时使用）
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def read_template_options(template_path, sheet_names) -> dict:
    """读取母版中选项 sheet（options_8/30/31/35）A 列的全部值。

    作为规则引擎的数据池：下拉长列表以母版为准，字典永不过期。
    :return: {sheet_name: [选项1, 选项2, ...]}
    """
    wb = openpyxl.load_workbook(template_path, data_only=True, read_only=True)
    pools = {}
    try:
        for sheet_name in sheet_names:
            if sheet_name not in wb.sheetnames:
                raise ValueError(f"母版中缺少选项 sheet: {sheet_name}")
            ws = wb[sheet_name]
            values = [row[0] for row in ws.iter_rows(values_only=True) if row[0] not in (None, "")]
            if not values:
                raise ValueError(f"母版选项 sheet {sheet_name} 为空")
            pools[sheet_name] = values
    finally:
        wb.close()
    return pools


class ExcelBatchWriter:
    """按官方模板分批生成导入用 xlsx

    :param template_path: 母版路径（data/templates/asset_daoru.xlsx）
    :param sheet_name: 数据 sheet 名（应用系统）
    :param output_dir: 输出目录
    :param file_prefix: 输出文件名前缀（asset_app_001.xlsx ...）
    :param data_start_row: 数据起始行，默认 3（1 表头 + 2 后端样例保留）
    """

    def __init__(self, template_path, sheet_name,
                 output_dir, file_prefix, data_start_row: int = 3):
        self.template_path = Path(template_path)
        if not self.template_path.is_file():
            raise FileNotFoundError(f"导入母版不存在: {template_path}")
        self.sheet_name = sheet_name
        # 表头以母版第 1 行为准（构造时读取一次，写入时还会再做严格校验）
        self.field_titles = self._read_header_titles()
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.file_prefix = file_prefix
        self.data_start_row = data_start_row

    def _read_header_titles(self) -> list:
        """读取母版数据 sheet 第 1 行全部表头"""
        wb = openpyxl.load_workbook(self.template_path, read_only=True)
        try:
            if self.sheet_name not in wb.sheetnames:
                raise ValueError(f"母版中缺少数据 sheet: {self.sheet_name}")
            ws = wb[self.sheet_name]
            first = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
            titles = [v for v in first if v is not None]
            if not titles:
                raise ValueError("母版第 1 行表头为空")
            return titles
        finally:
            wb.close()

    def write_all(self, factory, count: int, rows_per_file: int, grouping: dict) -> list:
        """全量分批生成，返回输出文件路径列表。

        每批：分组造数（组不会被拆到两个文件）→ 加载母版 → 第 3 行起写入 → 另存。
        每个文件独立加载母版，写入完成即释放，百万行不会全量驻留内存。
        """
        output_files = []
        remaining = count
        file_idx = 1
        while remaining > 0:
            batch = min(rows_per_file, remaining)
            columns, groups = factory.build_grouped_rows(batch, grouping)
            rows = [row for group in groups for row in group]
            assert len(rows) == batch, f"本批行数异常: 期望 {batch}，实际 {len(rows)}"

            out_path = self.output_dir / f"{self.file_prefix}{file_idx:03d}.xlsx"
            self._write_one(columns, rows, out_path)
            output_files.append(str(out_path))

            remaining -= batch
            file_idx += 1
            logger.info("已生成 %d/%d 行，文件: %s", count - remaining, count, out_path.name)
        return output_files

    def _write_one(self, columns, rows, out_path):
        """加载母版 → 校验表头 → 扩展下拉区域 → 写入数据 → 保存为新文件"""
        wb = openpyxl.load_workbook(self.template_path)
        if self.sheet_name not in wb.sheetnames:
            raise ValueError(f"母版中缺少数据 sheet: {self.sheet_name}")
        ws = wb[self.sheet_name]

        # 列数必须与母版表头一致（防止 fields 漏配/多配导致错位列）
        if len(columns) != len(self.field_titles):
            raise ValueError(
                f"字段数 {len(columns)} 与母版表头数 {len(self.field_titles)} 不一致，"
                f"请检查表定义中的 fields"
            )
        # 表头严格校验：母版第 1 行必须与构造时读取的表头全等
        headers = [ws.cell(1, c).value for c in range(1, len(self.field_titles) + 1)]
        if headers != self.field_titles:
            raise ValueError("母版表头在运行期间发生变化，请重新检查母版文件")
        # columns 顺序即母版列序
        col_index = {name: i + 1 for i, name in enumerate(columns)}

        # 数据起始行（第 3 行）；样例第 2 行原样保留
        last_row = self.data_start_row + len(rows) - 1
        self._extend_validations(ws, last_row)

        for offset, row in enumerate(rows):
            excel_row = self.data_start_row + offset
            for name, value in zip(columns, row):
                ws.cell(excel_row, col_index[name]).value = self._to_cell(value)

        wb.save(out_path)
        wb.close()
        logger.info("文件已保存: %s（数据行 %d~%d，共 %d 行）",
                    out_path.name, self.data_start_row, last_row, len(rows))

    def _extend_validations(self, ws, last_row: int):
        """把母版下拉数据校验区域扩展到数据末行（从第 3 行起，避开表头/样例行）。

        母版 sqref 形如 C2:C1001（含样例行）；扩展后为 C3:C{last_row}。
        样例行仍在原校验区域覆盖范围内（第 2 行），不受影响。
        """
        for dv in ws.data_validations.dataValidation:
            # 一个校验对象可能覆盖多列/多区域，逐区域取列字母重建
            new_ranges = []
            for ref in str(dv.sqref).split():
                min_col, _, max_col, _ = range_boundaries(ref)
                for col_idx in range(min_col, max_col + 1):
                    letter = get_column_letter(col_idx)
                    new_ranges.append(f"{letter}{self.data_start_row}:{letter}{last_row}")
            dv.sqref = " ".join(new_ranges)

    @staticmethod
    def _to_cell(value):
        """把生成值转为 Excel 单元格值：

        - None → 空单元格
        - date/datetime → 'YYYY-MM-DD' 文本（与模板样例的文本日期一致）
        - 数值/布尔 → 字符串（模板中数量等列样例均为文本，保证导入解析一致）
        """
        if value is None:
            return None
        if isinstance(value, (_dt.date, _dt.datetime)):
            return value.strftime("%Y-%m-%d")
        if isinstance(value, (list, dict)):
            return str(value)
        if not isinstance(value, str):
            return str(value)
        return value
