"""Draw editable vector artwork for the public project page. All content synthetic."""
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "docs/assets"
root.mkdir(parents=True, exist_ok=True)

cover = '''<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="550" viewBox="0 0 1600 550" role="img" aria-labelledby="title desc">
<title id="title">kexam — понять решение, обосновать балл</title>
<desc id="desc">Обложка проекта проверки рукописной математики. Справа — стилизованная математическая запись.</desc>
<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#f5dfed"/><stop offset=".55" stop-color="#e7eaff"/><stop offset="1" stop-color="#dfeeff"/></linearGradient></defs>
<rect width="1600" height="550" rx="28" fill="url(#bg)"/>
<g font-family="Arial, sans-serif"><text x="78" y="88" font-size="41" font-weight="700" fill="#27334b"><tspan fill="#344dcc">k</tspan>exam<tspan dx="22" dy="-5" fill="#77819b" font-size="14" font-weight="400">ПРОВЕРКА РЕШЕНИЙ ЕГЭ</tspan></text></g>
<g font-family="Georgia, 'Times New Roman', serif" font-size="69" fill="#263bb0"><text x="76" y="230">Понять решение.</text><text x="76" y="315">Обосновать балл.</text></g>
<g font-family="Arial, sans-serif" fill="#44516c" font-size="23"><text x="80" y="387">Рукопись → математический разбор → критерии</text><text x="80" y="476" font-size="17" letter-spacing="1.5">№13 / №15 · КОД · МЕТОДИКА · РЕЗУЛЬТАТЫ</text></g>
<g transform="translate(1040 83) rotate(5 220 190)">
<rect width="455" height="371" rx="23" fill="#fff" fill-opacity=".86"/>
<path d="M35 85H420M35 138H420M35 191H420M35 244H420M35 297H420" stroke="#edf0fa" stroke-width="1.5"/>
<g font-family="Georgia, serif" fill="#283654" font-size="29"><text x="38" y="64">Что здесь обосновано?</text><text x="39" y="123">sin x = 0</text><text x="39" y="177">x = πk, k ∈ ℤ</text><text x="39" y="230">x ∈ [0; π]</text><text x="39" y="283">Ответ: 0; π</text></g>
<path d="M341 258l12 12 25-31" stroke="#344dcc" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
<rect x="34" y="314" width="384" height="34" rx="17" fill="#e9edff"/><text x="51" y="336" font-family="Arial, sans-serif" font-size="15" fill="#344dcc">У каждой оценки должно быть основание.</text>
</g>
<path d="M959 178 C1009 117 1018 317 964 342" fill="none" stroke="#8797de" stroke-width="2" stroke-dasharray="5 9"/>
</svg>'''

product = '''<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="650" viewBox="0 0 1440 650" role="img" aria-labelledby="title desc">
<title id="title">Искусственный пример разбора решения</title>
<desc id="desc">Ученик правильно определил знаки дроби, но включил в ответ ноль числителя при строгом неравенстве. Предложен один балл, замечание связано с ответом, совет отделён от основания.</desc>
<rect width="1440" height="650" rx="24" fill="#eef1fc"/>
<g font-family="Arial, sans-serif"><text x="44" y="53" font-size="21" font-weight="700" fill="#29344f"><tspan fill="#344dcc">k</tspan>exam</text><text x="1395" y="51" text-anchor="end" font-size="13" fill="#68748e">ИСКУССТВЕННЫЙ ПРИМЕР · НЕ РЕЗУЛЬТАТ МОДЕЛИ</text>
<text x="44" y="113" font-size="33" fill="#29344f">Разбор твоего решения</text></g>
<rect x="40" y="146" width="701" height="456" rx="22" fill="white"/>
<g font-family="Arial, sans-serif"><text x="68" y="186" font-size="15" fill="#75809a">ЗАДАНИЕ 15</text><text x="710" y="186" text-anchor="end" font-size="15" fill="#75809a">Страница 1</text></g>
<g font-family="Georgia, 'Times New Roman', serif" fill="#29344f">
<text x="71" y="243" font-size="26">Решите: (x − 1) / (x + 2) &gt; 0</text>
<text x="71" y="299" font-size="25">ОДЗ: x ≠ −2. Нуль числителя: x = 1.</text>
<path d="M84 370H669" stroke="#66718a" stroke-width="2"/><path d="M669 370l-11-5m11 5-11 5" stroke="#66718a" stroke-width="2"/>
<circle cx="273" cy="370" r="6" fill="white" stroke="#29344f" stroke-width="2"/><circle cx="492" cy="370" r="6" fill="white" stroke="#29344f" stroke-width="2"/>
<text x="259" y="407" font-size="23">−2</text><text x="487" y="407" font-size="23">1</text>
<text x="167" y="350" font-size="27">+</text><text x="374" y="350" font-size="27">−</text><text x="566" y="350" font-size="27">+</text>
<rect x="64" y="454" width="647" height="64" rx="12" fill="#f8e8ec"/>
<text x="81" y="495" font-size="27">Ответ: (−∞; −2) ∪ [1; +∞)</text>
</g>
<g font-family="Arial, sans-serif"><circle cx="686" cy="456" r="16" fill="#344dcc"/><text x="686" y="461" text-anchor="middle" font-size="14" fill="white">1</text><text x="72" y="564" font-size="14" fill="#75809a">Запись сохранена так, как её написал условный ученик.</text></g>
<rect x="766" y="146" width="634" height="117" rx="22" fill="white"/>
<g font-family="Arial, sans-serif" fill="#29344f"><text x="794" y="184" font-size="16">Предложенный балл</text><text x="1359" y="227" text-anchor="end" font-size="47" fill="#344dcc">1 / 2</text><text x="794" y="225" font-size="15" fill="#66718a">Неверно включена граничная точка</text></g>
<rect x="766" y="284" width="634" height="318" rx="22" fill="white"/>
<g font-family="Arial, sans-serif" fill="#29344f"><text x="795" y="323" font-size="14" fill="#344dcc">1 · ВЛИЯЕТ НА БАЛЛ</text><text x="795" y="366" font-size="28">Скобка у точки 1</text>
<text x="795" y="410" font-size="20">При x = 1 дробь равна нулю.</text><text x="795" y="441" font-size="20">Неравенство строгое — точка не входит.</text>
<path d="M795 470H1370" stroke="#e6eaf4"/>
<text x="795" y="508" font-size="16" fill="#66718a">КАК ИСПРАВИТЬ</text><text x="795" y="544" font-size="20">Замени [1; +∞) на (1; +∞).</text><text x="795" y="575" font-size="17" fill="#66718a">Остальную часть ответа сохрани.</text></g>
</svg>'''
(root / "cover.svg").write_text(cover)
(root / "product-example.svg").write_text(product)
print("Vector cover and synthetic example rendered")
