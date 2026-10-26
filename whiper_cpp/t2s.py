import sys
from opencc import OpenCC

cc = OpenCC('t2s')  # 繁体 -> 简体
for path in sys.argv[1:]:
    with open(path, 'r', encoding='utf-8', newline='') as f:
        text = f.read()
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(cc.convert(text))
