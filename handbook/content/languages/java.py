ARTICLES = {
    'java-overview': {
        'title': 'Java 概览与模板',
        'summary': 'Main 类、包、竞赛结构',
        'content': r'''
## 提交要求

公共类名必须为 **`Main`**，与文件名一致。本 OJ 使用 Java 在沙箱中编译运行。

## 推荐模板

```java
import java.util.*;
import java.io.*;

public class Main {
    static final int INF = 0x3f3f3f3f;

    public static void main(String[] args) throws Exception {
        BufferedReader br = new BufferedReader(
            new InputStreamReader(System.in));
        PrintWriter out = new PrintWriter(
            new BufferedOutputStream(System.out));

        // solve

        out.flush();
    }
}
```

## 与 C++ 的差异

- 强类型，必须声明类型
- 无指针运算，数组与集合用类封装
- 大数组注意 **对象** 与 **基本类型** 数组的区别
''',
    },
    'java-types-ops': {
        'title': 'Java 类型与运算',
        'summary': '基本类型、包装类、运算符',
        'content': r'''
## 基本类型

| 类型 | 字节 | 说明 |
|------|------|------|
| `int` | 4 | 常用 |
| `long` | 8 | 加 `L`：`1e18L` |
| `double` | 8 | 浮点 |
| `boolean` | - | true/false |
| `char` | 2 | 单字符 |

## 包装类

`Integer`、`Long` 等用于集合泛型；自动装箱拆箱。

## 运算

```java
int a = 7 / 3;      // 2 整除
long s = 1L * a * b; // 防 int 溢出
```

## 取模

`(a + b) % MOD` 注意 `a`、`b` 先转 long。

## 比较

基本类型用 `==`；**字符串**用 `s1.equals(s2)`，不要用 `==`。

## 位运算

与 C++ 相同：`& | ^ ~ << >>`。
''',
    },
    'java-control': {
        'title': 'Java 判断与循环',
        'summary': 'if、switch、for、while',
        'content': r'''
## if / else

```java
if (x > 0) {
} else if (x == 0) {
} else {
}
```

## switch

```java
switch (x) {
    case 1:
        break;
    case 2:
        break;
    default:
        break;
}
```

Java 12+ 可用 `switch` 表达式（视环境而定）。

## for

```java
for (int i = 0; i < n; i++) { }

int[] a = {1, 2, 3};
for (int x : a) { }  // 增强 for

for (String line; (line = br.readLine()) != null; ) { }
```

## while

```java
while (n-- > 0) { }
```

## break / continue

与 C++ 相同。
''',
    },
    'java-io': {
        'title': 'Java 输入输出',
        'summary': 'BufferedReader、PrintWriter、StringTokenizer',
        'content': r'''
## 快速读入（推荐）

```java
BufferedReader br = new BufferedReader(
    new InputStreamReader(System.in));
StringTokenizer st = new StringTokenizer(br.readLine());
int n = Integer.parseInt(st.nextToken());
int m = Integer.parseInt(st.nextToken());
```

下一行：

```java
st = new StringTokenizer(br.readLine());
```

## 输出

```java
PrintWriter out = new PrintWriter(
    new BufferedOutputStream(System.out));
out.println(ans);
out.print(x + " ");
out.flush();
```

## Scanner（慎用）

写法简单但大数据易 **TLE**，仅小数据或试代码时用。

## 读单个字符

一般读整行再解析；`br.read()` 按字符读。
''',
    },
    'java-arrays-string': {
        'title': 'Java 数组与字符串',
        'summary': '一维、二维、String、StringBuilder',
        'content': r'''
## 数组

```java
int[] a = new int[n];
int[] b = new int[]{1, 2, 3};
Arrays.fill(a, 0);
Arrays.sort(a);
```

二维：

```java
int[][] g = new int[n][m];
```

## String

不可变。

```java
String s = "abc";
s.length();
s.charAt(i);
s.substring(l, r);
s.indexOf("a");
s.split(" ");
```

拼接大量字符用 **StringBuilder**：

```java
StringBuilder sb = new StringBuilder();
sb.append(x).append(' ');
sb.toString();
```

## 字符与数字

```java
char c = '7';
int d = c - '0';
```
''',
    },
    'java-collections': {
        'title': 'Java 集合框架',
        'summary': 'ArrayList、HashMap、TreeSet、PriorityQueue',
        'content': r'''
## ArrayList

```java
ArrayList<Integer> list = new ArrayList<>();
list.add(x);
list.get(i);
list.size();
Collections.sort(list);
```

## HashMap / HashSet

```java
HashMap<String, Integer> mp = new HashMap<>();
mp.put("a", 1);
mp.getOrDefault("b", 0);
mp.containsKey("a");

HashSet<Integer> st = new HashSet<>();
st.add(x); st.contains(x);
```

## TreeMap / TreeSet

有序，$O(\log n)$。

```java
TreeMap<Integer, Integer> tm = new TreeMap<>();
TreeSet<Long> ts = new TreeSet<>();
```

## PriorityQueue（堆）

```java
PriorityQueue<Integer> pq = new PriorityQueue<>();  // 小根堆
// 大根堆：new PriorityQueue<>(Collections.reverseOrder())
pq.offer(x); pq.poll(); pq.peek();
```

## Stack / Queue / Deque

```java
Deque<Integer> dq = new ArrayDeque<>();
dq.offerLast(x); dq.pollFirst();
```
''',
    },
}
