import requests
import re
import time
import logging
from loguru import logger
from openai import OpenAI
from .models import CourseRun, Task

class StepikSolver:
    def __init__(self, token, course_run, ai_client):
        self.token = token
        self.course_run = course_run
        self.ai_client = ai_client
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        })
        self.api_url = "https://stepik.org/api"

    def log(self, message, is_error=False):
        timestamp = time.strftime("%H:%M:%S")
        prefix = "[ERROR]" if is_error else "[INFO]"
        log_line = f"{timestamp} {prefix} {message}\n"
        self.course_run.log += log_line
        self.course_run.save(update_fields=['log'])
        if is_error:
            logger.error(message)
        else:
            logger.info(message)

    def get_course_score(self, course_id):
        r = self.session.get(f"{self.api_url}/courses/{course_id}")
        if r.status_code != 200:
            return 0, 0
        course = r.json()["courses"][0]
        progress_id = course.get("progress")
        if not progress_id:
            return 0, 0
        r = self.session.get(f"{self.api_url}/progresses/{progress_id}")
        prog = r.json()["progresses"][0]
        return int(prog.get("score", 0)), int(prog.get("cost", 0))

    def create_attempt(self, step_id):
        data = {"attempt": {"step": step_id}}
        r = self.session.post(f"{self.api_url}/attempts", json=data)
        if r.status_code != 201:
            self.log(f"Ошибка создания attempt: {r.text}", True)
            return None
        return r.json()["attempts"][0]

    def _clean_html(self, text):
        cleaned = re.sub(r'<[^>]+>', '', str(text))
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return cleaned

    def _normalize_text(self, text):
        """Нормализует текст для более точного сравнения."""
        text = self._clean_html(text).lower()
        # Убираем префиксы вроде "a)", "b)", "1.", "а)", "б)" в начале
        text = re.sub(r'^[a-zа-яёa-z0-9]{1,2}[\)\.\:]\s*', '', text)
        return text.strip()

    def _extract_option_text(self, opt):
        """
        Безопасно извлекает текст опции.
        Stepik может вернуть опцию либо строкой, либо словарём {'text': '...'}.
        """
        if isinstance(opt, str):
            return self._clean_html(opt)
        elif isinstance(opt, dict):
            # Пробуем популярные ключи
            for key in ("text", "name", "value"):
                if key in opt:
                    return self._clean_html(opt[key])
            return self._clean_html(str(opt))
        return self._clean_html(str(opt))

    def _parse_db_answer_lines(self, db_answer):
        """
        Разбивает db_answer на строки, пробуя сначала \n, потом запятую.
        Возвращает список непустых строк.
        """
        lines = [l.strip() for l in db_answer.split('\n') if l.strip()]
        if len(lines) <= 1:
            # Fallback: разбивка по запятой (но осторожно — запятая может быть в тексте)
            # Используем только если нет -> (matching) и нет | (table)
            comma_lines = [l.strip() for l in db_answer.split(',') if l.strip()]
            if len(comma_lines) > 1:
                lines = comma_lines
        return lines

    def check_db_answer(self, stepik_id):
        """Пытается найти ответ в базе данных."""
        try:
            task = Task.objects.filter(stepik_id=stepik_id).first()
            if task and task.correct_answer:
                return task.correct_answer
        except Exception as e:
            self.log(f"Ошибка поиска в БД: {e}", True)
        return None

    # --- AI Solvers ---

    def _build_wrong_answers_hint(self, wrong_answers):
        if not wrong_answers:
            return ""
        hint = "\n\nВНИМАНИЕ! Следующие ответы уже были даны и оказались НЕПРАВИЛЬНЫМИ. НЕ повторяй их, попробуй другой вариант:\n"
        for i, ans in enumerate(wrong_answers, 1):
            hint += f"--- Неправильный ответ {i} ---\n{ans}\n"
        return hint

    def ask_ai(self, prompt):
        resp = self.ai_client.chat.completions.create(
            model="gemma4",
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content.strip()

    # --- Submitters ---

    def submit_answer_request(self, attempt_id, reply_data):
        data = {
            "submission": {
                "attempt": attempt_id,
                "reply": reply_data
            }
        }
        r = self.session.post(f"{self.api_url}/submissions", json=data)
        if r.status_code == 201:
            return r.json()["submissions"][0]
        self.log(f"Ошибка отправки: {r.status_code} {r.text}", True)
        return None

    def wait_for_evaluation(self, submission_id):
        for _ in range(5):
            time.sleep(1)
            r = self.session.get(f"{self.api_url}/submissions/{submission_id}")
            if r.status_code == 200:
                status = r.json()["submissions"][0].get("status")
                if status != "evaluation":
                    return status
        return "evaluation"

    # --- Handlers ---

    def handle_choice(self, step_id, question, hint_text, db_answer, attempt=None):
        """
        attempt передаётся снаружи чтобы не создавать его дважды.
        Если не передан — создаём здесь.
        """
        if attempt is None:
            attempt = self.create_attempt(step_id)
        if not attempt:
            return False, None

        # FIX: используем _extract_option_text чтобы корректно обработать dict-опции
        raw_options = attempt["dataset"]["options"]
        clean_options = [self._extract_option_text(opt) for opt in raw_options]

        if db_answer:
            # --- Парсим db_answer: поддерживаем JSON (новый формат) и legacy строки ---
            import json as _json
            db_correct_texts = None  # список правильных текстов опций

            # Пробуем JSON-массив: ["opt1", "opt2"]
            stripped = db_answer.strip()
            if stripped.startswith('['):
                try:
                    parsed = _json.loads(stripped)
                    if isinstance(parsed, list):
                        db_correct_texts = [self._clean_html(str(x)) for x in parsed]
                except Exception:
                    pass

            # Legacy: несколько строк через \n (один ответ на строку)
            if db_correct_texts is None:
                lines = [p.strip() for p in db_answer.split('\n') if p.strip()]
                if len(lines) > 1:
                    db_correct_texts = lines
                else:
                    # Один ответ (или старый однострочный формат с запятой-разделителем)
                    # Оставляем как одну строку — точное совпадение либо token-match
                    db_correct_texts = lines  # может быть список из 1 элемента

            norm_db = [self._normalize_text(t) for t in db_correct_texts]

            # Для legacy однострочного формата (', '.join) строим padded-строку
            norm_db_full = self._normalize_text(db_answer)
            padded_db = ', ' + norm_db_full + ', '

            choices = []
            matched_opts = []
            for clean_opt in clean_options:
                norm_opt = self._normalize_text(clean_opt)
                if not norm_opt:
                    choices.append(False)
                    continue

                # Шаг 1: точное совпадение с любым элементом из распарсенного списка
                matched = norm_opt in norm_db

                # Шаг 2 (только для legacy однострочного формата):
                # token-match через ", " — защита от "Person" ⊂ "Personality"
                if not matched and len(norm_db) <= 1:
                    padded_opt = ', ' + norm_opt + ', '
                    matched = padded_opt in padded_db

                choices.append(matched)
                if matched:
                    matched_opts.append(clean_opt)

            self.log(f"Используем ответ из БД: {matched_opts}")
        else:
            prompt = (
                f"Ответь ТОЛЬКО точными текстами вариантов ответа из списка ниже "
                f"(скопируй их дословно). Если правильных ответов несколько — напиши каждый на отдельной строке.\n"
                f"Вопрос:\n{question}{hint_text}\n"
                f"Варианты:\n{chr(10).join(clean_options)}"
            )
            answer = self.ask_ai(prompt)
            raw_answers = [a.strip() for a in answer.split('\n') if a.strip()]
            self.log(f"AI ответ: {raw_answers}")

            choices = []
            for clean_opt in clean_options:
                norm_opt = self._normalize_text(clean_opt)
                matched = any(clean_opt == ans for ans in raw_answers)
                if not matched:
                    norm_ans_list = [self._normalize_text(a) for a in raw_answers]
                    matched = any(
                        norm_opt == na or (len(na) > 3 and (na in norm_opt or norm_opt in na))
                        for na in norm_ans_list
                    )
                choices.append(matched)

        if not any(choices):
            self.log("Ни один вариант не совпал точно!", True)
            return False, db_answer or "\n".join(clean_options)

        sub = self.submit_answer_request(attempt['id'], {"choices": choices})
        answer_text = "\n".join(opt for opt, chosen in zip(clean_options, choices) if chosen)
        return sub, answer_text if sub else None

    def handle_sorting(self, step_id, question, hint_text, db_answer, attempt=None):
        if attempt is None:
            attempt = self.create_attempt(step_id)
        if not attempt:
            return False, None

        # FIX: корректно извлекаем текст опций (могут быть dict)
        raw_options = attempt.get("dataset", {}).get("options", [])
        options = [self._extract_option_text(opt) for opt in raw_options]

        if db_answer:
            # FIX: пробуем \n, потом запятую; убираем нумерацию "1. ", "2) " и т.п.
            lines = self._parse_db_answer_lines(db_answer)
            sorted_items = [re.sub(r'^\d+[\.\)\:]\s*', '', line).strip() for line in lines]
            self.log(f"Используем ответ из БД: {sorted_items}")
        else:
            prompt = (
                f"Расставь следующие элементы в правильном порядке. "
                f"Ответь ТОЛЬКО списком элементов через перенос строки.\n"
                f"Задание:\n{question}{hint_text}\n"
                f"Элементы:\n{chr(10).join(options)}"
            )
            answer = self.ask_ai(prompt)
            sorted_items = [line.strip() for line in answer.split("\n") if line.strip()]
            self.log(f"AI ответ: {sorted_items}")

        ordering = []
        norm_options = [self._normalize_text(opt) for opt in options]
        for item in sorted_items:
            norm_item = self._normalize_text(item)
            matched = None
            # Точное совпадение
            for i, norm_opt in enumerate(norm_options):
                if norm_opt == norm_item:
                    matched = i
                    break
            # Подстрока
            if matched is None:
                for i, norm_opt in enumerate(norm_options):
                    if norm_item in norm_opt or norm_opt in norm_item:
                        matched = i
                        break
            if matched is not None:
                ordering.append(matched)

        if not ordering:
            return False, str(sorted_items)
        sub = self.submit_answer_request(attempt['id'], {"ordering": ordering})
        return sub, str(sorted_items) if sub else None

    def handle_text_based(self, step_id, question, hint_text, block_name, db_answer):
        attempt = self.create_attempt(step_id)
        if not attempt: return False, None

        if db_answer and block_name != 'free-answer':
            answer = db_answer
            self.log(f"Используем ответ из БД: {answer}")
        else:
            if block_name == 'number':
                prompt = f"Ответь ТОЛЬКО числом.\nВопрос:\n{question}{hint_text}"
            elif block_name == 'string':
                prompt = f"Ответь ТОЛЬКО точным ответом.\nВопрос:\n{question}{hint_text}"
            elif block_name == 'math':
                prompt = f"Ответь ТОЛЬКО математическим выражением или числом.\nВопрос:\n{question}{hint_text}"
            else:  # free-answer
                prompt = f"""
Дай развёрнутый ответ на вопрос.

Правила:
- Начинай сразу с ответа, без вступлений
- Запрещены любые мета-комментарии (про анализ, стиль, задание и т.д.)
- Не пиши шаблонные фразы ИИ
- Не добавляй пояснения вроде "я постараюсь", "ниже приведён ответ"
- Пиши естественно, как человек
- Без воды, только полезное содержание
- Ответ пиши на том языке, на котором задан вопрос (самое важное)

Вопрос:
{question}{hint_text}
"""
            answer = self.ask_ai(prompt)
            self.log(f"AI ответ: {answer[:100]}")

        reply = {}
        if block_name == 'number':
            reply = {"number": answer}
        elif block_name == 'math':
            reply = {"formula": answer}
        else:
            reply = {"text": answer, "files": []}

        sub = self.submit_answer_request(attempt['id'], reply)
        return sub, answer if sub else None

    def handle_matching(self, step_id, question, hint_text, db_answer, attempt=None):
        if attempt is None:
            attempt = self.create_attempt(step_id)
        if not attempt:
            return False, None

        pairs = attempt.get("dataset", {}).get("pairs", [])
        # FIX: корректно извлекаем текст из пар (могут содержать HTML или быть dict)
        firsts = [self._clean_html(p["first"]) for p in pairs]
        seconds = [self._clean_html(p["second"]) for p in pairs]

        if db_answer:
            lines = [l for l in db_answer.split('\n') if l.strip()]
            # Если всё в одной строке через запятую с '->'
            if len(lines) == 1 and '->' in lines[0] and ',' in lines[0]:
                lines = [l.strip() for l in db_answer.split(',') if '->' in l]
            self.log(f"Используем ответ из БД: {lines}")
        else:
            prompt = (
                f"Сопоставь элементы. Ответь ТОЛЬКО в формате: левый элемент = правый элемент\n"
                f"Задание:\n{question}{hint_text}\n"
                f"Левая колонка:\n{chr(10).join(firsts)}\n"
                f"Правая колонка:\n{chr(10).join(seconds)}"
            )
            answer = self.ask_ai(prompt)
            lines = [l for l in answer.split('\n') if '=' in l]
            self.log(f"AI ответ:\n{answer}")

        sep = '->' if db_answer else '='
        # Строим словарь: нормализованная левая часть -> нормализованная правая часть
        pairs_dict = {}
        for line in lines:
            if sep not in line:
                continue
            left, right = [x.strip() for x in line.split(sep, 1)]
            pairs_dict[self._normalize_text(left)] = self._normalize_text(right)

        self.log(f"Словарь пар: {pairs_dict}")

        norm_seconds = [self._normalize_text(s) for s in seconds]
        ordering = []

        for first in firsts:
            norm_first = self._normalize_text(first)

            # Ищем правую часть по точному совпадению ключа
            mapped_right_norm = pairs_dict.get(norm_first)

            # FIX: если не нашли точно — пробуем подстроку
            if not mapped_right_norm:
                for k, v in pairs_dict.items():
                    if k and norm_first and (k in norm_first or norm_first in k):
                        mapped_right_norm = v
                        break

            if not mapped_right_norm:
                self.log(f"Не удалось найти пару для: '{first}'", True)
                ordering.append(0)
                continue

            # Ищем индекс правой части в seconds
            right_idx = None
            # Точное совпадение
            for i, norm_sec in enumerate(norm_seconds):
                if norm_sec == mapped_right_norm:
                    right_idx = i
                    break
            # Подстрока
            if right_idx is None:
                for i, norm_sec in enumerate(norm_seconds):
                    if norm_sec and mapped_right_norm and (
                        norm_sec in mapped_right_norm or mapped_right_norm in norm_sec
                    ):
                        right_idx = i
                        break

            if right_idx is not None:
                ordering.append(right_idx)
            else:
                self.log(f"Не удалось найти индекс для правой части: '{mapped_right_norm}'", True)
                self.log(f"  Доступные seconds (norm): {norm_seconds}", True)
                ordering.append(0)

        if len(ordering) != len(pairs):
            self.log("Количество пар не совпадает (fallback).", True)
            return False, "\n".join(lines)

        sub = self.submit_answer_request(attempt['id'], {"ordering": ordering})
        return sub, "\n".join(lines) if sub else None

    def handle_table(self, step_id, question, hint_text, db_answer, attempt=None):
        if attempt is None:
            attempt = self.create_attempt(step_id)
        if not attempt:
            return False, None

        dataset = attempt.get("dataset", {})
        rows = dataset.get("rows", [])
        columns = dataset.get("columns", [])
        is_checkbox = dataset.get("is_checkbox", False)

        if db_answer:
            lines = db_answer.split('\n')
            self.log(f"Используем ответ из БД")
        else:
            prompt = (
                f"Заполни таблицу. "
                f"{'В каждой строке несколько ответов.' if is_checkbox else 'В каждой строке один ответ.'} "
                f"Ответь ТОЛЬКО в формате: строка | столбец1 | столбец2\n"
                f"Задание:\n{question}{hint_text}\n"
                f"Строки: {', '.join(rows)}\n"
                f"Столбцы: {', '.join(columns)}"
            )
            answer = self.ask_ai(prompt)
            lines = [l.strip() for l in answer.split("\n") if "|" in l]
            self.log(f"AI ответ:\n{answer}")

        matrix = [[False] * len(columns) for _ in range(len(rows))]

        for line in lines:
            if db_answer:
                if ':' not in line:
                    continue
                row_name, cols_str = line.split(':', 1)
                selected_cols = [c.strip() for c in cols_str.split(',')]

                row_idx = next((i for i, r in enumerate(rows) if r.strip() == row_name.strip()), None)
                if row_idx is None:
                    continue

                for col_name in selected_cols:
                    col_idx = next((i for i, c in enumerate(columns) if c.strip() == col_name.strip()), None)
                    if col_idx is not None:
                        matrix[row_idx][col_idx] = True
            else:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 2:
                    continue
                row_name, values = parts[0], parts[1:]

                row_idx = next((i for i, r in enumerate(rows) if r.strip() == row_name.strip()), None)
                if row_idx is None:
                    row_idx = next(
                        (i for i, r in enumerate(rows) if r.strip() in row_name or row_name in r.strip()),
                        None
                    )
                if row_idx is None:
                    continue

                for col_idx, val in enumerate(values):
                    if col_idx >= len(columns):
                        break
                    matrix[row_idx][col_idx] = "+" in val

        sub = self.submit_answer_request(attempt['id'], {"choices": matrix})
        return sub, "\n".join(lines) if sub else None

    def handle_fill_blanks(self, step_id, question, hint_text, db_answer, attempt=None):
        if attempt is None:
            attempt = self.create_attempt(step_id)
        if not attempt:
            return False, None

        components = attempt.get("dataset", {}).get("components", [])
        blanks_count = sum(1 for c in components if c["type"] == "input")

        if db_answer:
            if blanks_count == 1:
                answers = [db_answer.strip()]
            else:
                answers = [a.strip() for a in db_answer.split(',')]
            self.log(f"Используем ответ из БД: {answers}")
        else:
            display_text = ""
            blank_num = 0
            for comp in components:
                if comp["type"] == "text":
                    display_text += comp["text"]
                elif comp["type"] == "input":
                    blank_num += 1
                    display_text += f"[ПРОПУСК_{blank_num}]"

            clean_text = self._clean_html(re.sub(r'<br\s*/?>', '\n', display_text))
            prompt = (
                f"В тексте есть пропуски. Вставь пропущенные слова. "
                f"Ответь ТОЛЬКО списком значений — по одному на строку.\n"
                f"Задание:\n{question}{hint_text}\n"
                f"Текст:\n{clean_text}"
            )
            answer = self.ask_ai(prompt)
            answers = [line.strip() for line in answer.split("\n") if line.strip()]
            self.log(f"AI ответ: {answers}")

        if len(answers) > blanks_count:
            answers = answers[:blanks_count]
        else:
            answers.extend([''] * (blanks_count - len(answers)))

        sub = self.submit_answer_request(attempt['id'], {"blanks": answers})
        return sub, ", ".join(answers) if sub else None

    def mark_step_as_viewed(self, step_id):
        data = {"view": {"step": step_id}}
        r = self.session.post(f"{self.api_url}/views", json=data)
        if r.status_code in (200, 201, 204):
            self.log(f"Шаг {step_id} помечен как просмотренный ✓")
        else:
            self.log(f"Ошибка при пометке шага {step_id}", True)

    def process_step(self, step_id, step, max_attempts=3):
        block_name = step["block"]["name"]

        if block_name in ("video", "text"):
            self.mark_step_as_viewed(step_id)
            return True

        question = self._clean_html(step["block"]["text"])
        db_answer = self.check_db_answer(step_id)

        attempts_allowed = 1 if db_answer and block_name != 'free-answer' else max_attempts
        wrong_answers = []

        for attempt_num in range(1, attempts_allowed + 1):
            hint_text = self._build_wrong_answers_hint(wrong_answers)
            sub, current_answer = None, None

            # FIX: создаём attempt ОДИН раз здесь и передаём в handler
            # (раньше process_step создавал attempt И handler создавал ещё один)
            att = self.create_attempt(step_id)
            if not att:
                self.log("Не удалось создать attempt.", True)
                break

            if block_name == "choice":
                sub, current_answer = self.handle_choice(step_id, question, hint_text, db_answer, attempt=att)

            elif block_name == "sorting":
                sub, current_answer = self.handle_sorting(step_id, question, hint_text, db_answer, attempt=att)

            elif block_name in ("number", "string", "free-answer", "math"):
                # text_based создаёт свой attempt внутри (не требует dataset из attempt)
                sub, current_answer = self.handle_text_based(step_id, question, hint_text, block_name, db_answer)

            elif block_name == "matching":
                sub, current_answer = self.handle_matching(step_id, question, hint_text, db_answer, attempt=att)

            elif block_name == "table":
                sub, current_answer = self.handle_table(step_id, question, hint_text, db_answer, attempt=att)

            elif block_name == "fill-blanks":
                sub, current_answer = self.handle_fill_blanks(step_id, question, hint_text, db_answer, attempt=att)

            else:
                self.log(f"Пропуск неизвестного типа: {block_name}")
                return True

            if not sub:
                self.log("Ответ не сформирован.", True)
                if current_answer:
                    wrong_answers.append(current_answer)
                continue

            status = self.wait_for_evaluation(sub['id'])

            if status == "correct":
                self.log(f"Правильно! ✓")
                return True
            else:
                self.log(f"Неправильно (status={status})")
                if current_answer:
                    wrong_answers.append(current_answer)

                if db_answer:
                    self.log("Ответ из БД оказался неверным, переключаемся на AI", True)
                    db_answer = None
                    attempts_allowed = max_attempts

        self.log(f"Не удалось решить задание {step_id}", True)
        return False

    def run(self):
        try:
            self.course_run.status = 'running'
            self.course_run.save()

            self.log(f"Запуск автопрохождения курса {self.course_run.course.stepik_id}...")

            r = self.session.get(f"{self.api_url}/courses/{self.course_run.course.stepik_id}")
            course = r.json()["courses"][0]

            all_steps = []
            for section_id in course["sections"]:
                r = self.session.get(f"{self.api_url}/sections/{section_id}")
                for unit_id in r.json()["sections"][0]["units"]:
                    r = self.session.get(f"{self.api_url}/units/{unit_id}")
                    lesson_id = r.json()["units"][0]["lesson"]
                    r = self.session.get(f"{self.api_url}/lessons/{lesson_id}")
                    for index, step_id in enumerate(r.json()["lessons"][0]["steps"]):
                        all_steps.append((step_id, lesson_id, index))

            self.course_run.steps_total = len(all_steps)
            self.course_run.save()
            self.log(f"Всего шагов в курсе: {len(all_steps)}")

            unpassed_steps = []
            batch_size = 20
            for i in range(0, len(all_steps), batch_size):
                batch = all_steps[i:i + batch_size]
                step_ids = [s[0] for s in batch]

                ids_param = "&".join([f"ids[]={sid}" for sid in step_ids])
                r = self.session.get(f"{self.api_url}/steps?{ids_param}")
                steps_data = {s["id"]: s for s in r.json()["steps"]}

                progress_ids = [s.get("progress") for s in steps_data.values() if s.get("progress")]

                passed_progress_ids = set()
                if progress_ids:
                    prog_param = "&".join([f"ids[]={pid}" for pid in progress_ids])
                    r = self.session.get(f"{self.api_url}/progresses?{prog_param}")
                    for prog in r.json()["progresses"]:
                        if prog.get("is_passed"):
                            passed_progress_ids.add(prog["id"])

                for step_id, lesson_id, index in batch:
                    step_data = steps_data.get(step_id)
                    if not step_data:
                        continue
                    if step_data.get("progress") in passed_progress_ids:
                        continue
                    unpassed_steps.append((step_id, lesson_id, index, step_data))

            self.course_run.steps_done = len(all_steps) - len(unpassed_steps)
            self.course_run.save()
            self.log(f"Пройдено: {self.course_run.steps_done}/{len(all_steps)} | Осталось: {len(unpassed_steps)}")

            if not unpassed_steps:
                self.log("Все шаги уже пройдены!")
                self.course_run.status = 'completed'
                self.course_run.save()
                return

            current_score, total_score = self.get_course_score(self.course_run.course.stepik_id)
            self.course_run.current_score = current_score
            self.course_run.total_score = total_score
            self.course_run.target_score = int(total_score * (self.course_run.target_percent / 100))
            self.course_run.save()

            for step_num, (step_id, lesson_id, index, step) in enumerate(unpassed_steps, 1):
                self.course_run.refresh_from_db()
                if self.course_run.status == 'stopped':
                    self.log("Прохождение остановлено пользователем.")
                    return

                current_score, _ = self.get_course_score(self.course_run.course.stepik_id)
                self.course_run.current_score = current_score
                self.course_run.save()

                if current_score >= self.course_run.target_score:
                    self.log(
                        f"🎯 Цель достигнута! Текущий балл: {current_score} "
                        f"(цель была {self.course_run.target_score}). Переключаемся в режим «только просмотр»."
                    )
                    for _, (s_id, l_id, idx, s) in enumerate(unpassed_steps[step_num - 1:]):
                        if s["block"]["name"] in ("video", "text"):
                            self.mark_step_as_viewed(s_id)
                            self.course_run.steps_done += 1
                            self.course_run.save()
                    break

                step_url = f"https://stepik.org/lesson/{lesson_id}/step/{index + 1}"
                block_name = step["block"]["name"]
                self.log(f"[{step_num}/{len(unpassed_steps)}] Обрабатываем: {step_url} | тип: {block_name}")

                self.process_step(step_id, step)

                self.course_run.steps_done += 1
                self.course_run.save()

            self.log("Курс полностью пройден!")
            self.course_run.status = 'completed'
            self.course_run.save()

        except Exception as e:
            self.log(f"Критическая ошибка: {str(e)}", True)
            self.course_run.status = 'failed'
            self.course_run.save()