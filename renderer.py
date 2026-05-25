import json
import os

import jinja2
import pdfkit

import config
from utils import setup_logger


class ResumeRenderer:
    def __init__(self) -> None:
        self.logger = setup_logger("renderer")
        with open(config.RESUME_TEMPLATE_PATH, encoding="utf-8") as f:
            template_src = f.read()
        self.template = jinja2.Environment(
            autoescape=False,
            undefined=jinja2.Undefined,
        ).from_string(template_src)
        os.makedirs(config.OUTPUT_RESUME_DIR, exist_ok=True)

        # Configure pdfkit — use path from config if wkhtmltopdf isn't on PATH
        wk_path = config.WKHTMLTOPDF_PATH
        self.pdf_config = (
            pdfkit.configuration(wkhtmltopdf=wk_path)
            if wk_path and os.path.exists(wk_path)
            else None
        )
        self._pdf_options = {
            "page-size": "A4",
            "margin-top": "15mm",
            "margin-right": "15mm",
            "margin-bottom": "15mm",
            "margin-left": "15mm",
            "encoding": "UTF-8",
            "no-outline": None,
            "quiet": "",
        }

    def render(self, resume_data: dict, job_id: str) -> str:
        html = self.template.render(resume=resume_data)
        output_path = os.path.join(
            config.OUTPUT_RESUME_DIR, f"{job_id}_resume.pdf"
        )
        kwargs = {"options": self._pdf_options}
        if self.pdf_config:
            kwargs["configuration"] = self.pdf_config
        pdfkit.from_string(html, output_path, **kwargs)
        self.logger.info(f"Rendered PDF: {output_path}")
        return output_path

    def render_from_file(self, json_path: str) -> str:
        with open(json_path, encoding="utf-8") as f:
            resume_data = json.load(f)
        job_id = os.path.basename(json_path).replace("_resume.json", "")
        return self.render(resume_data, job_id)
