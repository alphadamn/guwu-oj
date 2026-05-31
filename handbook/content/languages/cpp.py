ARTICLES = {
    'cpp-overview': {
        'title': 'C++ 概览与模板',
        'summary': '头文件、命名空间、竞赛常用写法',
        'content': r'''
## 为什么用 C++

运行快、STL 强大，是 OJ 与 ICPC 最主流选择。本 OJ 支持 C++17 沙箱评测。

## 推荐模板

```cpp
#include <bits/stdc++.h>  // 部分环境可用；正式比赛建议按需 include
#include <iostream>
#include <vector>
#include <algorithm>
using namespace std;

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);
    return 0;
}
```

按需包含常用头文件：`#include <queue>`、`#include <map>` 等。

## 命名空间

`using namespace std;` 可省略 `std::` 前缀。避免 `using namespace std;` 与自定义名冲突。

## 编译常见选项

- `-std=c++17`、`-O2`（OJ 一般已配置）
- 本地：`g++ -std=c++17 -O2 -o main main.cpp`
''',
    },
    'cpp-types-ops': {
        'title': 'C++ 类型与运算',
        'summary': '整型、浮点、字符、运算符与类型转换',
        'content': r'''
## 基本类型

| 类型 | 约范围 | 说明 |
|------|--------|------|
| `int` | $\pm 2\times 10^9$ | 默认整数 |
| `long long` | $\pm 9\times 10^{18}$ | **乘法、前缀和常用** |
| `unsigned` | $0 \sim 4\times 10^9$ | 仅非负时可用 |
| `double` | 约 15 位有效数字 | 几何、实数题 |
| `char` | 单字符 | 也可当小整数 |
| `bool` | true/false | |

## 变量与常量

```cpp
int n = 10;
const int MAXN = 1e5 + 10;
long long ans = 0;
```

## 算术运算

```cpp
int a = 7, b = 3;
a / b;   // 整除得 2
a % b;   // 余数 1
```

**取模**：`(a + b) % MOD` 建议写成 `(a % MOD + b % MOD) % MOD`，防溢出。

## 自增与复合赋值

`++i`、`i++`（循环中通常无差别）；`+=`、`-=`、`*=`。

## 类型转换

```cpp
double x = 1.0 * a / b;  // 实数除法
int k = (int)3.14;       // 截断
```

## 比较

`==` `!=` `<` `>` `<=` `>=`，结果是 `bool`。

## 位运算（常用）

| 运算 | 含义 |
|------|------|
| `x & 1` | 奇偶 |
| `x >> 1` | 除以 2 |
| `x << k` | 乘 $2^k$ |
| `x & (-x)` | 取最低位 1（lowbit） |
| `x & (x-1)` | 去掉最低位 1 |

状压 DP、子集枚举会大量用到。
''',
    },
    'cpp-control': {
        'title': 'C++ 判断与循环',
        'summary': 'if / switch、for / while、break 与 continue',
        'content': r'''
## 分支 if / else

```cpp
if (x > 0) {
    // ...
} else if (x == 0) {
    // ...
} else {
    // ...
}
```

三元运算符：`int m = (a > b) ? a : b;`

## switch

适合多分支等值判断（整数、字符）：

```cpp
switch (op) {
    case 1: /* ... */ break;
    case 2: /* ... */ break;
    default: break;
}
```

## for 循环

```cpp
for (int i = 0; i < n; i++) { /* ... */ }

vector<int> v = {1, 2, 3};
for (int x : v) { /* 范围 for */ }
```

## while

```cpp
while (cin >> n && n) { /* 多组数据 */ }
while (!q.empty()) { /* BFS */ }
```

## break / continue

- `break`：跳出当前循环或 switch
- `continue`：进入下一轮循环

## 常见写法

```cpp
// 枚举子集（状压）
for (int s = mask; s; s = (s - 1) & mask) { }

// 二分
while (l < r) {
    int mid = (l + r + 1) >> 1;
    if (check(mid)) l = mid; else r = mid - 1;
}
```
''',
    },
    'cpp-io': {
        'title': 'C++ 输入输出',
        'summary': 'cin/cout、scanf/printf、快读与格式',
        'content': r'''
## 标准流（推荐入门）

```cpp
int n;
cin >> n;
cout << n << '\n';  // 用 \n，少用 endl
```

一行多个数：

```cpp
int a, b;
cin >> a >> b;
```

## 加速

```cpp
ios::sync_with_stdio(false);
cin.tie(nullptr);
```

## scanf / printf

```cpp
scanf("%d", &n);
printf("%d\n", ans);
printf("%lld\n", (long long)ans);  // long long 用 %lld
```

## 文件重定向（本地调试）

```cpp
freopen("in.txt", "r", stdin);
freopen("out.txt", "w", stdout);
```

## 读字符串

```cpp
string s;
cin >> s;           // 不含空格
getline(cin, s);    // 含空格，注意前面若有 cin>> 需 getchar 吃掉换行
```

## 输出格式

```cpp
cout << fixed << setprecision(2) << x;  // 需 #include <iomanip>
```
''',
    },
    'cpp-arrays-string': {
        'title': 'C++ 数组与字符串',
        'summary': '静态数组、vector、string 操作',
        'content': r'''
## 静态数组

```cpp
const int N = 1e5 + 10;
int a[N];
int b[N][20];  // 二维注意空间
memset(a, 0, sizeof a);      // 按字节填充，常用于 0 / -1
fill(a, a + n, 0);           // C++ 填 0
```

全局数组在 BSS 段，**比局部大数组更安全**（避免栈溢出）。

## vector

```cpp
vector<int> v(n);           // 长度 n，初值 0
vector<int> v(n, -1);       // 初值 -1
v.push_back(x);
v.pop_back();
v.size();
v.empty();
v.clear();
v.resize(m);
```

二维：`vector<vector<int>> g(n, vector<int>(m));`

## string

```cpp
string s = "abc";
s += "d";
s.size(); s[i];
s.substr(pos, len);
s.find("pat");  // 找不到返回 string::npos
```

字符串与数字：`to_string(x)`、`stoi(s)`、`stoll(s)`。

## 字符

`'a'` 与 `"a"` 不同；`'0' + digit` 转字符。
''',
    },
    'cpp-stl-containers': {
        'title': 'C++ STL 容器',
        'summary': 'set、map、堆、栈、队列、deque',
        'content': r'''
## pair / tuple

```cpp
pair<int,int> p = {1, 2};
p.first; p.second;
make_pair(1, 2);
```

## set / multiset

有序，去重（set），$O(\log n)$ 插入删除。

```cpp
set<int> s;
s.insert(x); s.erase(x); s.count(x);
for (int x : s) { }
```

## map

```cpp
map<string, int> mp;
mp["key"]++;
if (mp.count("key")) { }
```

需要更快哈希：C++11 起 `unordered_map`（均摊 O(1)）。

## priority_queue（堆）

```cpp
priority_queue<int> pq;  // 大根堆
priority_queue<int, vector<int>, greater<int>> pq;  // 小根堆
pq.push(x); pq.top(); pq.pop();
```

## stack / queue / deque

```cpp
stack<int> st;
queue<int> q;
deque<int> dq;  // 双端队列，单调队列常用
```
''',
    },
    'cpp-stl-algorithm': {
        'title': 'C++ algorithm 库',
        'summary': 'sort、二分、排列、最值',
        'content': r'''
## 排序

```cpp
sort(a, a + n);
sort(v.begin(), v.end());
sort(v.begin(), v.end(), greater<int>());  // 降序
stable_sort(/* 稳定排序 */);
```

## 二分

```cpp
// 有序序列中第一个 >= x
int i = lower_bound(a, a + n, x) - a;
// 第一个 > x
int j = upper_bound(a, a + n, x) - a;
```

## 最值与求和

```cpp
*max_element(a, a + n);
*min_element(v.begin(), v.end());
accumulate(a, a + n, 0LL);
```

## 翻转、去重

```cpp
reverse(v.begin(), v.end());
sort(v.begin(), v.end());
v.erase(unique(v.begin(), v.end()), v.end());
```

## 排列

```cpp
next_permutation(a, a + n);  // 字典序下一个
```

## 其他

`swap(a,b)`、`min(a,b)`、`max(a,b)`、`__gcd(a,b)`（C++17）。
''',
    },
}
