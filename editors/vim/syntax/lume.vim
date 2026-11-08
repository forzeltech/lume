" Vim syntax file for Lume (.lm)
if exists("b:current_syntax")
  finish
endif

syn keyword lumeKeyword     if else while for in return break continue defer spawn
syn keyword lumeMemory      region keep
syn keyword lumeStorage     let var const fn struct
syn keyword lumeModifier    mut pub
syn keyword lumeInclude     import extern test
syn keyword lumeConstant    true false nil
syn keyword lumeType        i8 i16 i32 i64 u8 u16 u32 u64 f32 f64 bool str Task
syn match   lumeType        "\<error\>\(\s*(\)\@!"
syn match   lumeBuiltin     "\<\(print\|eprint\|panic\|assert\|assert_eq\|error\|argc\|arg\|now_ns\|mem_live\|read_file\|write_file\|read_stdin\)\ze\s*("
syn match   lumeUserType    "\<[A-Z][A-Za-z0-9_]*\>"
syn match   lumeFuncDecl    "\<fn\s\+\zs[A-Za-z_][A-Za-z0-9_]*"
syn match   lumeStructDecl  "\<struct\s\+\zs[A-Za-z_][A-Za-z0-9_]*"
syn match   lumeCall        "\<[a-z_][A-Za-z0-9_]*\ze\s*("

syn match   lumeNumber      "\<0[xX][0-9a-fA-F_]\+\>"
syn match   lumeNumber      "\<0[bB][01_]\+\>"
syn match   lumeNumber      "\<[0-9][0-9_]*\(\.[0-9][0-9_]*\)\=\([eE][+-]\=[0-9]\+\)\=\>"
syn match   lumeByte        "'\(\\[nrt0\\'\"]\|[^\\']\)'"
syn match   lumeWrapOp      "+%\|-%\|\*%"
syn match   lumeOperator    "\.\.\|==\|!=\|<=\|>=\|&&\|||"

syn match   lumeEscape      contained "\\[nrt0\\\"{}]"
syn region  lumeInterp      contained matchgroup=lumeInterpDelim start="{" end="}" contains=TOP
syn region  lumeString      start=+"+ skip=+\\\\\|\\"+ end=+"+ contains=lumeEscape
syn region  lumeIString     matchgroup=lumeIPrefix start=+\<i"+ skip=+\\\\\|\\"+ end=+"+ contains=lumeEscape,lumeInterp

syn keyword lumeTodo        contained TODO FIXME XXX NOTE
syn match   lumeComment     "//.*$" contains=lumeTodo
syn region  lumeComment     start="/\*" end="\*/" contains=lumeTodo

hi def link lumeKeyword     Statement
hi def link lumeMemory      Special
hi def link lumeStorage     StorageClass
hi def link lumeModifier    StorageClass
hi def link lumeInclude     Include
hi def link lumeConstant    Constant
hi def link lumeType        Type
hi def link lumeUserType    Type
hi def link lumeBuiltin     Function
hi def link lumeFuncDecl    Function
hi def link lumeStructDecl  Type
hi def link lumeCall        Function
hi def link lumeNumber      Number
hi def link lumeByte        Character
hi def link lumeString      String
hi def link lumeIString     String
hi def link lumeIPrefix     Special
hi def link lumeEscape      SpecialChar
hi def link lumeInterpDelim Delimiter
hi def link lumeWrapOp      Operator
hi def link lumeOperator    Operator
hi def link lumeComment     Comment
hi def link lumeTodo        Todo

let b:current_syntax = "lume"
