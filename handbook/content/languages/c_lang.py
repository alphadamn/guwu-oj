ARTICLES = {
    'c-overview': {
        'title': 'C 语言概览',
        'summary': '适用场景与和 C++ 的关系',
        'content': r'''
## 说明

C 是面向过程语言，无 STL。本 OJ **暂不自动评测 C**，建议使用 **C++** 提交（可混用 `scanf/printf`）。

## 最小模板

```c
#include <stdio.h>
#include <stdlib.h>

int main(void) {
    int n;
    scanf("%d", &n);
    printf("%d\n", n);
    return 0;
}
```

## 头文件

`stdio.h`（IO）、`stdlib.h`（malloc）、`string.h`（memset）、`math.h`（数学函数）。

## 学习建议

竞赛以 C++ 为主；理解 C 有助于理解指针与内存，见后续章节。
''',
    },
    'c-types-ops': {
        'title': 'C 类型与运算',
        'summary': '整型、char、运算符',
        'content': r'''
## 类型

| 类型 | 说明 |
|------|------|
| `int` | 整数 |
| `long long` | 长整型，格式 `%lld` |
| `double` | `%lf` |
| `char` | 字符 |

## 变量

```c
int a = 10;
const int MAX = 1e5;
```

## 运算

与 C++ 类似：`+ - * / %`，整除向零截断。

## 自增

`++i`、`i++`。

## 类型转换

`(double)a / b` 得到浮点商。
''',
    },
    'c-control': {
        'title': 'C 判断与循环',
        'summary': 'if、switch、for、while',
        'content': r'''
## if / else

```c
if (x > 0) {
} else if (x == 0) {
} else {
}
```

## switch

```c
switch (op) {
    case 1: break;
    default: break;
}
```

## for / while

```c
for (int i = 0; i < n; i++) { }

while (scanf("%d", &n) == 1 && n) { }
```

## break / continue

与 C++ 相同。
''',
    },
    'c-io': {
        'title': 'C 输入输出',
        'summary': 'scanf、printf、格式说明符',
        'content': r'''
## scanf

```c
int n;
scanf("%d", &n);           // 必须取地址 &
scanf("%d%d", &a, &b);
scanf("%lld", &x);         // long long
scanf("%s", s);            // 字符串无空格
```

## printf

```c
printf("%d\n", n);
printf("%lld\n", x);
printf("%.2f\n", d);
printf("%s\n", s);
```

## 注意

- `&` 不能漏
- `long long` 与 `int` 格式符别混用
''',
    },
    'c-arrays-pointers': {
        'title': 'C 数组与指针',
        'summary': '数组、字符串、指针基础',
        'content': r'''
## 数组

```c
int a[100];
int b[10][10];
memset(a, 0, sizeof(a));
```

下标从 0 开始，注意越界。

## 字符串

字符数组：

```c
char s[100];
scanf("%s", s);
strlen(s);
strcpy(dst, src);
strcmp(a, b);
```

## 指针（了解）

```c
int x = 10;
int *p = &x;
*p = 20;
```

动态内存：`malloc` / `free`（竞赛中少用，多用静态或 vector 的 C++）。

## 向函数传数组

数组名即首元素地址，常配合长度 `n` 传递。
''',
    },
}
