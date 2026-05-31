ARTICLES = {
    'python-overview': {
        'title': 'Python 概览与模板',
        'summary': '版本、环境、竞赛常用结构',
        'content': r'''
## 特点

语法简洁、内置大整数，适合原型与数据范围较小的题。本 OJ 使用 **Python 3**。

## 推荐模板

```python
import sys
input = sys.stdin.readline

def solve():
    pass

if __name__ == '__main__':
    solve()
```

## 缩进

用 **4 空格**缩进，同一代码块层级必须一致。冒号 `:` 后开始新块。

## 模块导入

```python
import sys
from collections import deque, Counter, defaultdict
from heapq import heappush, heappop
```

## 注意

Python 常数大，$O(n^2)$ 在 $n=10^5$ 时通常 TLE；优先确认复杂度。
''',
    },
    'python-types-ops': {
        'title': 'Python 类型与运算',
        'summary': 'int、float、bool、运算符',
        'content': r'''
## 基本类型

| 类型 | 说明 |
|------|------|
| `int` | 任意精度整数 |
| `float` | 双精度浮点 |
| `bool` | `True` / `False` |
| `str` | 字符串，不可变 |
| `None` | 空值 |

## 算术

```python
7 // 3   # 整除 2
7 % 3    # 余数 1
2 ** 10  # 幂 1024
```

## 比较与逻辑

```python
a == b; a != b; a < b
x and y; x or y; not x
```

## 链式比较

```python
if 0 <= x < n:  # 合法
```

## 赋值

```python
a, b = 1, 2
a, b = b, a   # 交换
x += 1
```

## 成员与身份

```python
x in lst
a is b        # 同一对象（慎用）
```

## 位运算

与 C++ 相同：`& | ^ ~ << >>`，用于状压、子集枚举。
''',
    },
    'python-control': {
        'title': 'Python 判断与循环',
        'summary': 'if、for、while、推导式',
        'content': r'''
## if / elif / else

```python
if x > 0:
    pass
elif x == 0:
    pass
else:
    pass
```

## for 循环

```python
for i in range(n):          # 0 .. n-1
    pass

for i in range(1, n + 1):   # 1 .. n
    pass

for i, x in enumerate(a):   # 带下标
    pass

for x in lst:               # 遍历元素
    pass
```

## while

```python
while n > 0:
    n //= 2
```

## break / continue / else

`for` 带 `else`：未 `break` 时执行（不常用）。

## 列表推导式

```python
squares = [x * x for x in range(10)]
evens = [x for x in a if x % 2 == 0]
```

## 多组数据

```python
import sys
data = sys.stdin.read().split()
it = iter(data)
# 或逐行 while True: line = input(); if not line: break
```
''',
    },
    'python-io': {
        'title': 'Python 输入输出',
        'summary': 'input、快读、格式化输出',
        'content': r'''
## 基本读入

```python
n = int(input())
a, b = map(int, input().split())
lst = list(map(int, input().split()))
```

## 快读

```python
import sys
input = sys.stdin.readline
n = int(input())
line = input().rstrip('\n')
```

## 一次读入全部

```python
import sys
nums = list(map(int, sys.stdin.read().split()))
```

## 输出

```python
print(x)
print(a, b)           # 空格分隔
print(f"{a} {b}")     # f-string
print(' '.join(map(str, lst)))
```

大量输出用 `sys.stdout.write`。

## 格式

```python
print(f"{pi:.2f}")
print("{:05d}".format(x))  # 宽度补零
```
''',
    },
    'python-strings-lists': {
        'title': 'Python 列表与字符串',
        'summary': '切片、常用方法',
        'content': r'''
## 列表 list

可变、有序。

```python
a = [1, 2, 3]
a.append(4)
a.pop()
a[-1]              # 最后一个
len(a)
a[1:4]             # 切片 [1,2,3]
a[::-1]            # 反转
a.sort()           # 原地
sorted(a)          # 新列表
```

## 字符串 str

```python
s = "hello"
s + " world"
s * 3
s[0]; s[-1]
s[2:5]
"a,b".split(",")
",".join(lst)
s.find("ll")
s.replace("a", "b")
s.strip()
```

## 元组 tuple

不可变，可作 dict 键：`p = (1, 2)`。

## 字典 dict

见「标准库」一章。
''',
    },
    'python-stdlib': {
        'title': 'Python 标准库（竞赛常用）',
        'summary': 'collections、heapq、bisect、math',
        'content': r'''
## collections

```python
from collections import deque, Counter, defaultdict

q = deque()
q.append(x); q.popleft()   # BFS 队列 O(1)

cnt = Counter(a)
cnt[x] += 1

dd = defaultdict(list)
dd[key].append(val)
```

## heapq（堆）

```python
import heapq
h = []
heapq.heappush(h, x)   # 小根堆
heapq.heappop(h)
# 大根堆：压入 -x
```

## bisect（二分）

```python
import bisect
i = bisect.bisect_left(a, x)   # 第一个 >= x
i = bisect.bisect_right(a, x)  # 第一个 > x
```

## math

```python
import math
math.gcd(a, b)
math.isqrt(n)      # Python 3.8+ 整数开方
math.inf
```

## itertools

`permutations`、`combinations`、`product` 用于暴力枚举。
''',
    },
}
