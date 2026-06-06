ARTICLES = {
    'assembly-overview': {
        'title': 'Assembly 概览',
        'summary': '汇编语言基础与 GNU as 语法（ARM64）',
        'content': r'''
## 说明

汇编语言是低级语言，直接对应机器指令。本 OJ 支持 ARM64 汇编（GNU as 语法）。

## 最小模板

```asm
.section .text
.global _start

_start:
    ; 退出程序
    mov x0, #0       ; exit code 0
    mov x8, #93      ; sys_exit on ARM64
    svc #0          ; system call
```

## 编译

本地编译命令：
```bash
as -o main.o main.s
ld -o main main.o
./main
```

## 寄存器

| 寄存器 | 用途 |
|--------|------|
| `x0`-`x7` | 参数/返回值 |
| `x8` | 系统调用号 |
| `x9`-`x15` | 临时寄存器 |
| `x19`-`x28` | 被调用者保存 |
| `x29` (fp) | 帧指针 |
| `x30` (lr) | 链接寄存器 |
| `sp` | 栈指针 |
''',
    },
    'assembly-io': {
        'title': 'Assembly 输入输出',
        'summary': '系统调用与 IO 操作',
        'content': r'''
## 系统调用

Linux ARM64 系统调用通过 `svc #0` 指令：

| 系统调用 | x8 | 参数 |
|----------|-----|------|
| sys_write | 64 | x0=fd, x1=buf, x2=count |
| sys_read | 63 | x0=fd, x1=buf, x2=count |
| sys_exit | 93 | x0=error_code |

## 使用 C 库函数（推荐）

使用 `scanf` 和 `printf` 更方便：

```asm
.data
    input_fmt: .asciz "%d %d"
    output_fmt: .asciz "%d\n"
    a: .word 0
    b: .word 0

.text
.global main

main:
    stp x29, x30, [sp, #-16]!    ; save frame pointer and return address
    mov x29, sp                  ; set frame pointer

    sub sp, sp, #16              ; allocate space for a and b

    add x0, sp, #8               ; &a
    add x1, sp, #12              ; &b
    ldr x2, =input_fmt           ; format string
    bl scanf                     ; scanf("%d %d", &a, &b)

    ldr w0, [sp, #8]             ; load a
    ldr w1, [sp, #12]            ; load b
    add w0, w0, w1               ; sum = a + b

    mov w1, w0                   ; sum as second argument
    ldr x0, =output_fmt          ; format string
    bl printf                    ; printf("%d\n", sum)

    mov w0, #0                   ; return 0
    ldp x29, x30, [sp], #16      ; restore frame pointer and return address
    ret
```

## 纯系统调用输出字符串

```asm
.data
    msg: .ascii "Hello, World!\n"
    len = . - msg

.text
.global _start

_start:
    mov x0, #1          ; stdout
    ldr x1, =msg        ; buffer address
    ldr x2, =len        ; length
    mov x8, #64         ; sys_write
    svc #0

    mov x0, #0          ; exit code 0
    mov x8, #93         ; sys_exit
    svc #0
```
''',
    },
    'assembly-data': {
        'title': 'Assembly 数据与内存',
        'summary': '数据段、栈操作',
        'content': r'''
## 数据段

```asm
.data
    num1: .word 42      ; 32位整数
    num2: .xword 100    ; 64位整数
    str: .ascii "test\0" ; 字符串
```

## 栈操作

```asm
stp x29, x30, [sp, #-16]!  ; 压栈
ldp x29, x30, [sp], #16   ; 出栈
```

## 内存访问

```asm
ldr x0, [x1]        ; 读取内存
str x0, [x1]        ; 写入内存
```
''',
    },
}
