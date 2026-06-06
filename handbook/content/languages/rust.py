ARTICLES = {
    'rust-overview': {
        'title': 'Rust 概览与模板',
        'summary': '特点、环境、竞赛常用结构',
        'content': r'''
## 特点

系统级编程语言，内存安全且无垃圾回收。本 OJ 使用 **Rust 2021 edition**。

## 推荐模板

```rust
use std::io::{self, BufRead, Write, BufWriter};

fn main() {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());

    // 快读示例
    let mut input = String::new();
    stdin.lock().read_line(&mut input).unwrap();
    let n: i32 = input.trim().parse().unwrap();

    // 循环处理
    for _ in 0..n {
        // 读取输入
        // 处理逻辑
        // 输出结果
        writeln!(out, "Result").unwrap();
    }
}
```

## 基本类型

| 类型 | 说明 |
|------|------|
| `i32` / `i64` | 有符号整数（推荐用 `i32`） |
| `u32` / `u64` | 无符号整数 |
| `f64` | 浮点数（推荐） |
| `bool` | 布尔值 |
| `String` | 可变字符串 |
| `&str` | 字符串切片（不可变） |

## 变量与可变性

```rust
let x = 5;              // 不可变
let mut y = 10;         // 可变
let z = &x;             // 不可变引用
let w = &mut y;         // 可变引用
```

## 控制流

与 C/C++ 类似：
```rust
if condition { }
else if condition { }
else { }

match expression {
    1 => println!("one"),
    2 => println!("two"),
    _ => println!("other"),
}

for i in 0..n {
    // 循环
}
```

## 输入输出

```rust
use std::io::{self, Read};

// 逐行读取
let mut line = String::new();
io::stdin().read_line(&mut line).unwrap();
let n: i32 = line.trim().parse().unwrap();

// 批量读取
let stdin = io::stdin();
let numbers: Vec<i32> = stdin.lock().lines()
    .map(|l| l.unwrap().trim().parse().unwrap())
    .collect();

// 格式化输出
println!("{}", n);
println!("{} {}", a, b);
```

## 通用技巧

- 使用 `BufWriter` 加速输出
- 使用 `Vec` 存储数据
- 使用 `sort()` 对数组排序
- 使用 `sort_by_key()` 按键排序
''',
    },
    'rust-strings-collections': {
        'title': 'Rust 字符串与集合',
        'summary': 'String、Vec、HashMap',
        'content': r'''
## String 与 &str

```rust
let s = String::from("hello");
let slice: &str = &s;  // 字符串切片
let part = &s[0..3];   // "hel"
```

## Vec 动态数组

```rust
let mut vec: Vec<i32> = Vec::new();
vec.push(1);
vec.push(2);
vec.push(3);

let len = vec.len();
let is_empty = vec.is_empty();
let first = vec[0];   // 直接访问
let first_opt = vec.get(0);  // 安全访问
vec.pop();            // 弹出最后一个元素
vec.remove(0);        // 删除指定位置
```

## 循环与迭代器

```rust
let vec = vec![1, 2, 3, 4, 5];

// for 循环
for item in vec {
    println!("{}", item);
}

// 带索引
for (i, item) in vec.iter().enumerate() {
    println!("{}: {}", i, item);
}

// 过滤
let evens: Vec<&i32> = vec.iter().filter(|&&x| x % 2 == 0).collect();

// 映射
let squares: Vec<i32> = vec.iter().map(|&&x| x * x).collect();
```

## HashMap 哈希表

```rust
use std::collections::HashMap;

let mut map = HashMap::new();
map.insert("a", 1);
map.insert("b", 2);
map.insert("c", 3);

if let Some(&value) = map.get("a") {
    println!("a = {}", value);
}

// 遍历
for (key, value) in &map {
    println!("{}: {}", key, value);
}
```
''',
    },
    'rust-stdlib': {
        'title': 'Rust 标准库（竞赛常用）',
        'summary': '迭代器、算法',
        'content': r'''
## 迭代器常用方法

```rust
let vec = vec![1, 2, 3, 4, 5];

// 遍历
vec.iter()              // 借用迭代器
vec.iter_mut()          // 可变借用迭代器
vec.into_iter()         // 消耗式迭代器

// 累积
vec.iter().fold(0, |acc, &x| acc + x);  // 求和
vec.iter().max();      // 最大值
vec.iter().min();      // 最小值

// 过滤与映射
vec.iter()
    .filter(|&&x| x % 2 == 0)  // 偶数
    .map(|&x| x * x)          // 平方
    .collect::<Vec<i32>>();   // 收集为 Vec

// 算法
vec.iter().sum::<i32>();       // 求和
vec.iter().product::<i32>();   // 乘积
```

## 排序

```rust
let mut vec = vec![5, 3, 1, 4, 2];

// 升序
vec.sort();

// 降序
vec.sort_by(|a, b| b.cmp(a));

// 按键排序
vec.sort_by_key(|x| x % 10);
```

## 库引用

```rust
use std::io::{self, BufRead};
use std::collections::{HashMap, VecDeque};
```
''',
    },
}
