import sys
sys.path.insert(0, ".")
from core.structure import validate_structure

regex_with_brackets = "const pattern = /^\\.\\//;\nconst items = list.filter(function(f) {\n  return pattern.test(f.name);\n});\nconst other = a / b / c;\nconst cls = /[({]/;\n"
print("regex sample:", validate_structure("app.js", regex_with_brackets))

good = "const pattern = /^\\.\\//;\nfunction f() { return 1; }\n"
print("good:", validate_structure("app.js", good))

bad = "const pattern = /^\\.\\//;\nfunction f() { return 1; \n"
print("bad:", validate_structure("app.js", bad))
