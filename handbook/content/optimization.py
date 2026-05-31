CATEGORY = {
    'title': '优化技巧',
    'icon': 'bi-lightning-charge',
    'description': 'IO、常数、内存、防 TLE/MLE 的实战建议。',
    'articles': {
        'io': {
            'title': '输入输出加速',
            'summary': '各语言快读快写要点',
            'content': r'''
## C++

```cpp
ios::sync_with_stdio(false);
cin.tie(nullptr);
```

大量输出：

```cpp
// 用 '\n' 代替 endl
// 或 fwrite 批量写缓冲区
```

读入整数可用 `scanf` 或手写 `read()` 按字节解析。

## Python

```python
import sys
input = sys.stdin.readline
```

写：

```python
sys.stdout.write('\n'.join(map(str, ans)) + '\n')
```

## Java

`BufferedReader` + `StringTokenizer` 读，`BufferedOutputStream` + `PrintWriter` 写。

## 本 OJ 注意

评测在 Docker 沙箱中运行，IO 仍是主要常数之一；数据量接近限制时务必优化。
''',
        },
        'constants': {
            'title': '常数优化',
            'summary': '卡常检查清单',
            'content': r'''
## 算法层面

- 降低复杂度阶：$O(n^2) \to O(n \log n)$
- 避免重复计算：前缀和、哈希、记忆化
- 换更合适的数据结构：vector 代替 list，数组代替 map（值域小时）

## 实现层面

| 技巧 | 说明 |
|------|------|
| 局部性 | 连续内存访问更快 |
| 位运算 | `x & 1` 代替 `% 2` |
| 内联简单函数 | `inline`（编译器也常自动内联） |
| 避免多余拷贝 | 引用传递 `const vector<int>&` |
| 全局数组 | 避免大局部数组栈溢出 |

## C++ 特有

- `reserve(n)` 预分配 vector
- `emplace_back` 代替 `push_back`（复杂对象）
- 链式前向星存图比 `vector<vector<int>>` 更省常数（边很多时）

## 对拍与压测

本地用大数据随机测，与暴力或 `std` 对拍，提交前估算运行时间。
''',
        },
        'memory': {
            'title': '内存优化',
            'summary': 'MLE 排查与空间压缩',
            'content': r'''
## 估算

- `int a[1000000]` ≈ 4 MB
- `long long` 数组 ×8
- 二维 `n×m` 注意 $n \times m$ 是否超限

题目「内存限制 256 MB」包含程序、栈、堆。

## 压缩方法

1. **滚动数组**：DP 只保留两行
2. **位压缩**：状态用 `int` 的每一位表示
3. **short / char**：值域小时用更小类型
4. **不要存多余信息**：只保留 DP 必需状态

## 递归

DFS 深度过大导致栈溢出：改迭代、手动栈，或 `vector` 模拟递归。

## Python

避免创建过多中间列表；用生成器；`del` 大对象（通常不必，但注意引用）。
''',
        },
        'debug': {
            'title': '调试与对拍',
            'summary': 'WA/TLE/RE 系统排查',
            'content': r'''
## 评测状态含义

| 状态 | 含义 |
|------|------|
| AC | 全部测试点通过 |
| WA | 输出与标准答案不一致 |
| TLE | 超出时间限制 |
| MLE | 超出内存限制 |
| RE | 运行时错误（越界、除零等） |
| CE | 编译错误 |

本 OJ 提交详情页可查看**各测试点**状态与运行时间。

## 调试流程

1. **CE**：看编译错误信息，检查语法与类名（Java）
2. **RE**：检查数组越界、除零、递归深度
3. **WA**：对拍小样例、检查边界（$n=0, n=1$）、特殊数据
4. **TLE**：估复杂度、优化 IO 与常数

## 对拍脚本思路

1. 数据生成器 `gen.py`
2. 暴力程序 `brute.cpp`
3. 你的程序 `sol.cpp`
4. 循环随机测，输出不一致则保存数据

## 常见边界

- 空输入、单元素、全相同、已排序/逆序
- `int` 溢出改 `long long`
- 浮点输出格式与精度
''',
        },
        'tips': {
            'title': '竞赛实战建议',
            'summary': '读题、写题、复盘',
            'content': r'''
## 读题

- 先读清输入输出格式与数据范围
- 数据范围决定算法复杂度上限
- 注意「多组数据」「求和 $\bmod$」等特殊要求

## 写题

1. 想清算法再写，必要时写伪代码
2. 先写暴力保证小样例，再优化
3. 模块化：读入、求解、输出分开

## 提交前检查

- [ ] 文件/类名正确（Java `Main`）
- [ ] 初始化变量（`memset`, `fill`, `INF`）
- [ ] 取模每一步是否写对
- [ ] 输出是否多换行/少空格

## 复盘

记录：题意、思路、错误原因、更优解法。配合本 OJ **题解** 功能与 OIer 手册查阅薄弱知识点。

## 学习路径建议

入门 → 模拟、排序、二分 → 搜索、贪心 → DP → 图论 → 数据结构 → 数学 → 综合提高。
''',
        },
    },
}
