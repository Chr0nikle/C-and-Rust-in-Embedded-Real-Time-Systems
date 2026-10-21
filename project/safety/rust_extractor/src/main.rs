//! Syntax-aware Rust structural extractor for the UASTW safety-analysis
//! prototype. It intentionally extracts facts only; it does not decide safety
//! contracts. The Python pipeline interprets these facts under explicit checks.

use proc_macro2::Span;
use quote::ToTokens;
use serde_json::{json, Value};
use std::collections::HashSet;
use std::env;
use std::fs;
use std::path::PathBuf;
use syn::punctuated::Punctuated;
use syn::spanned::Spanned;
use syn::visit::Visit;
use syn::{
    Attribute, Expr, ExprArray, ForeignItem, GenericArgument, Item, ItemConst, ItemFn, ItemStatic,
    Lit, LitStr, Meta, Pat, PathArguments, ReturnType, Type, TypePath,
};

#[derive(Clone, Copy, PartialEq, Eq)]
enum Truth {
    True,
    False,
    Unknown,
}

impl Truth {
    fn as_str(self) -> &'static str {
        match self {
            Truth::True => "true",
            Truth::False => "false",
            Truth::Unknown => "unknown",
        }
    }
    fn and(self, other: Truth) -> Truth {
        match (self, other) {
            (Truth::False, _) | (_, Truth::False) => Truth::False,
            (Truth::True, Truth::True) => Truth::True,
            _ => Truth::Unknown,
        }
    }
    fn or(self, other: Truth) -> Truth {
        match (self, other) {
            (Truth::True, _) | (_, Truth::True) => Truth::True,
            (Truth::False, Truth::False) => Truth::False,
            _ => Truth::Unknown,
        }
    }
    fn not(self) -> Truth {
        match self {
            Truth::True => Truth::False,
            Truth::False => Truth::True,
            Truth::Unknown => Truth::Unknown,
        }
    }
}

struct Cfg {
    arch: String,
    os: String,
    family: String,
    pointer_width: String,
    test: bool,
    features: HashSet<String>,
}

impl Cfg {
    fn eval_meta(&self, meta: &Meta) -> Truth {
        match meta {
            Meta::Path(path) => {
                if let Some(ident) = path.get_ident() {
                    match ident.to_string().as_str() {
                        "test" => {
                            if self.test { Truth::True } else { Truth::False }
                        }
                        "kani" | "unix" | "windows" => Truth::False,
                        "debug_assertions" => Truth::Unknown,
                        _ => Truth::Unknown,
                    }
                } else {
                    Truth::Unknown
                }
            }
            Meta::NameValue(nv) => {
                let Some(key) = nv.path.get_ident().map(ToString::to_string) else {
                    return Truth::Unknown;
                };
                let Expr::Lit(expr_lit) = &nv.value else {
                    return Truth::Unknown;
                };
                let Lit::Str(value) = &expr_lit.lit else {
                    return Truth::Unknown;
                };
                let value = value.value();
                match key.as_str() {
                    "target_arch" => bool_truth(value == self.arch),
                    "target_os" => bool_truth(value == self.os),
                    "target_family" => bool_truth(value == self.family),
                    "target_pointer_width" => bool_truth(value == self.pointer_width),
                    "feature" => bool_truth(self.features.contains(&value)),
                    _ => Truth::Unknown,
                }
            }
            Meta::List(list) => {
                let Some(op) = list.path.get_ident().map(ToString::to_string) else {
                    return Truth::Unknown;
                };
                let parsed = list.parse_args_with(Punctuated::<Meta, syn::Token![,]>::parse_terminated);
                let Ok(items) = parsed else {
                    return Truth::Unknown;
                };
                match op.as_str() {
                    "all" => items.iter().fold(Truth::True, |acc, m| acc.and(self.eval_meta(m))),
                    "any" => items.iter().fold(Truth::False, |acc, m| acc.or(self.eval_meta(m))),
                    "not" if items.len() == 1 => self.eval_meta(&items[0]).not(),
                    _ => Truth::Unknown,
                }
            }
        }
    }

    fn attrs(&self, attrs: &[Attribute]) -> (Truth, Vec<String>) {
        let mut truth = Truth::True;
        let mut raw = Vec::new();
        for attr in attrs {
            if attr.path().is_ident("cfg") {
                raw.push(attr.to_token_stream().to_string());
                match attr.parse_args::<Meta>() {
                    Ok(meta) => truth = truth.and(self.eval_meta(&meta)),
                    Err(_) => truth = truth.and(Truth::Unknown),
                }
            } else if attr.path().is_ident("cfg_attr") {
                raw.push(attr.to_token_stream().to_string());
                truth = truth.and(Truth::Unknown);
            }
        }
        (truth, raw)
    }
}

fn bool_truth(value: bool) -> Truth {
    if value { Truth::True } else { Truth::False }
}

fn line(span: Span) -> usize {
    span.start().line
}

fn column(span: Span) -> usize {
    span.start().column
}

fn tokens<T: ToTokens>(node: &T) -> String {
    node.to_token_stream().to_string()
}

fn last_ident(path: &syn::Path) -> String {
    path.segments
        .last()
        .map(|segment| segment.ident.to_string())
        .unwrap_or_else(|| tokens(path))
}

fn expr_is_call(expr: &Expr) -> bool {
    matches!(expr, Expr::Call(_) | Expr::MethodCall(_))
}

fn normalized_expr_text(expr: &Expr) -> String {
    let mut value = tokens(expr).trim().to_string();
    while value.starts_with('(') && value.ends_with(')') {
        value = value[1..value.len() - 1].trim().to_string();
    }
    value
}

fn expr_is_zero(expr: &Expr) -> bool {
    if expr_is_call(expr) {
        return false;
    }
    let value = normalized_expr_text(expr);
    let value = value.trim_end_matches(|c: char| matches!(c, 'u' | 'U' | 'l' | 'L'));
    value == "0"
}

fn expr_is_null(expr: &Expr) -> bool {
    if expr_is_call(expr) {
        return false;
    }
    matches!(normalized_expr_text(expr).to_lowercase().as_str(), "null" | "nullptr")
}

fn condition_shape(expr: &Expr) -> Value {
    match expr {
        Expr::Binary(binary) => json!({
            "kind": "binary",
            "operator": tokens(&binary.op),
            "left": tokens(binary.left.as_ref()),
            "right": tokens(binary.right.as_ref()),
            "left_call": expr_is_call(binary.left.as_ref()),
            "right_call": expr_is_call(binary.right.as_ref()),
            "left_zero": expr_is_zero(binary.left.as_ref()),
            "right_zero": expr_is_zero(binary.right.as_ref()),
            "left_null": expr_is_null(binary.left.as_ref()),
            "right_null": expr_is_null(binary.right.as_ref()),
        }),
        Expr::Unary(unary) => json!({
            "kind": "unary",
            "operator": tokens(&unary.op),
            "left": tokens(unary.expr.as_ref()),
            "right": Value::Null,
            "left_call": expr_is_call(unary.expr.as_ref()),
            "right_call": false,
            "left_zero": expr_is_zero(unary.expr.as_ref()),
            "right_zero": false,
            "left_null": expr_is_null(unary.expr.as_ref()),
            "right_null": false,
        }),
        Expr::MethodCall(method) => json!({
            "kind": "method_call",
            "operator": Value::Null,
            "left": tokens(&method.receiver),
            "right": Value::Null,
            "method": tokens(&method.method),
            "left_call": true,
            "right_call": false,
        }),
        Expr::Call(call) => json!({
            "kind": "call",
            "operator": Value::Null,
            "left": tokens(call),
            "right": Value::Null,
            "left_call": true,
            "right_call": false,
            "left_zero": false,
            "right_zero": false,
            "left_null": false,
            "right_null": false,
        }),
        Expr::Unsafe(_) | Expr::Block(_) => json!({
            "kind": "block",
            "operator": Value::Null,
            "left": tokens(expr),
            "right": Value::Null,
            "left_call": true,
            "right_call": false,
            "left_zero": false,
            "right_zero": false,
            "left_null": false,
            "right_null": false,
        }),
        _ => json!({
            "kind": "other",
            "operator": Value::Null,
            "left": tokens(expr),
            "right": Value::Null,
            "left_call": false,
            "right_call": false,
            "left_zero": expr_is_zero(expr),
            "right_zero": false,
            "left_null": expr_is_null(expr),
            "right_null": false,
        }),
    }
}

fn type_spec(ty: &Type) -> Value {
    match ty {
        Type::Path(path) => type_path_spec(path),
        Type::Ptr(ptr) => json!({
            "kind": "pointer",
            "const": ptr.const_token.is_some(),
            "mut": ptr.mutability.is_some(),
            "to": type_spec(&ptr.elem),
            "text": tokens(ty),
        }),
        Type::Reference(reference) => json!({
            "kind": "reference",
            "mut": reference.mutability.is_some(),
            "to": type_spec(&reference.elem),
            "text": tokens(ty),
        }),
        Type::BareFn(function) => {
            let abi = function
                .abi
                .as_ref()
                .and_then(|abi| abi.name.as_ref())
                .map(LitStr::value);
            json!({
                "kind": "function_pointer",
                "abi": abi,
                "params": function.inputs.iter().map(|input| type_spec(&input.ty)).collect::<Vec<_>>(),
                "ret": match &function.output {
                    ReturnType::Default => json!({"kind": "void"}),
                    ReturnType::Type(_, ty) => type_spec(ty),
                },
                "text": tokens(ty),
            })
        }
        Type::Array(array) => json!({
            "kind": "array",
            "len": tokens(&array.len),
            "elem": type_spec(&array.elem),
            "text": tokens(ty),
        }),
        Type::Tuple(tuple) if tuple.elems.is_empty() => json!({"kind": "void", "text": "()"}),
        Type::Tuple(tuple) => json!({
            "kind": "tuple",
            "elems": tuple.elems.iter().map(type_spec).collect::<Vec<_>>(),
            "text": tokens(ty),
        }),
        Type::Slice(slice) => json!({
            "kind": "slice",
            "elem": type_spec(&slice.elem),
            "text": tokens(ty),
        }),
        Type::Never(_) => json!({"kind": "never", "text": tokens(ty)}),
        _ => json!({"kind": "unsupported", "text": tokens(ty)}),
    }
}

fn type_path_spec(path: &TypePath) -> Value {
    let mut name = last_ident(&path.path);
    let mut args = Vec::new();
    if let Some(segment) = path.path.segments.last() {
        if let PathArguments::AngleBracketed(bracketed) = &segment.arguments {
            for arg in &bracketed.args {
                if let GenericArgument::Type(arg_ty) = arg {
                    args.push(type_spec(arg_ty));
                }
            }
        }
    }
    if name == "c_void" {
        name = "void".to_string();
    }
    json!({
        "kind": "named",
        "name": name,
        "args": args,
        "text": tokens(path),
    })
}

fn literal_usize(tokens: &str) -> Option<usize> {
    let cleaned = tokens.replace('_', "");
    let cleaned = cleaned
        .trim_end_matches("usize")
        .trim_end_matches("u32")
        .trim_end_matches("u64")
        .trim_end_matches("i32")
        .trim_end_matches("i64")
        .trim_end_matches('u')
        .trim_end_matches('i');
    cleaned.parse::<usize>().ok()
}

fn infer_array(expr: &Expr) -> Option<Value> {
    match expr {
        Expr::Array(ExprArray { elems, .. }) => {
            let len = elems.len();
            let elem = elems
                .first()
                .map(infer_value_type)
                .unwrap_or_else(|| json!({"kind": "unknown"}));
            Some(json!({
                "len": len,
                "len_expr": len.to_string(),
                "elem": elem,
                "elements": elems.iter().map(|e| tokens(e)).collect::<Vec<_>>(),
                "text": tokens(expr),
            }))
        }
        Expr::Repeat(repeat) => Some(json!({
            "len_expr": tokens(&repeat.len),
            "len": literal_usize(&tokens(&repeat.len)),
            "elem": infer_value_type(&repeat.expr),
            "elements": [],
            "text": tokens(expr),
        })),
        Expr::Cast(cast) => Some(json!({
            "len_expr": Value::Null,
            "elem": type_spec(&cast.ty),
            "elements": [],
            "text": tokens(expr),
        })),
        _ => None,
    }
}

fn infer_value_type(expr: &Expr) -> Value {
    match expr {
        Expr::Lit(lit) => match &lit.lit {
            Lit::Int(int) => {
                let suffix = int.suffix();
                if suffix.is_empty() {
                    json!({"kind": "named", "name": "integer_literal", "args": [], "text": tokens(expr)})
                } else {
                    json!({"kind": "named", "name": suffix, "args": [], "text": tokens(expr)})
                }
            }
            Lit::Bool(_) => json!({"kind": "named", "name": "bool", "args": [], "text": "bool"}),
            _ => json!({"kind": "unknown"}),
        },
        Expr::Cast(cast) => type_spec(&cast.ty),
        Expr::Array(array) => json!({
            "kind": "array",
            "len": array.elems.len(),
            "elem": array.elems.first().map(infer_value_type).unwrap_or_else(|| json!({"kind": "unknown"})),
            "text": tokens(expr),
        }),
        _ => json!({"kind": "unknown", "text": tokens(expr)}),
    }
}

fn pattern_binding(pat: &Pat) -> Option<(String, Option<Type>)> {
    match pat {
        Pat::Ident(ident) => Some((ident.ident.to_string(), None)),
        Pat::Type(typed) => {
            if let Pat::Ident(ident) = typed.pat.as_ref() {
                Some((ident.ident.to_string(), Some((*typed.ty).clone())))
            } else {
                None
            }
        }
        _ => None,
    }
}

fn local_fact(local: &syn::Local, active: &str) -> Option<Value> {
    let (name, explicit_ty) = pattern_binding(&local.pat)?;
    let init = local.init.as_ref();
    let init_expr = init.map(|init| init.expr.as_ref());
    let ty = explicit_ty.as_ref().map(type_spec).or_else(|| match init_expr {
        Some(expr) => {
            let inferred = infer_value_type(expr);
            if inferred.get("kind").and_then(Value::as_str) == Some("unknown") {
                None
            } else {
                Some(inferred)
            }
        }
        None => None,
    });
    Some(json!({
        "name": name,
        "type": ty,
        "line": line(local.span_start()),
        "init": init_expr.map(tokens),
        "array": init_expr.and_then(infer_array),
        "active": active,
    }))
}

trait SpanStart {
    fn span_start(&self) -> Span;
}
impl SpanStart for syn::Local {
    fn span_start(&self) -> Span {
        use syn::spanned::Spanned;
        self.span()
    }
}

fn array_info(ty: &Type, init: Option<&Expr>) -> Option<Value> {
    match ty {
        Type::Array(array) => {
            let mut value = json!({
                "len_expr": tokens(&array.len),
                "len": literal_usize(&tokens(&array.len)),
                "elem": type_spec(&array.elem),
                "elements": [],
                "text": tokens(ty),
            });
            if let Some(Expr::Array(ExprArray { elems, .. })) = init {
                value["elements"] = json!(elems.iter().map(tokens).collect::<Vec<_>>());
                value["len"] = json!(elems.len());
            }
            Some(value)
        }
        _ => None,
    }
}

fn const_fact(item: &ItemConst, scope: Option<&str>, active: &str, cfg_raw: &[String]) -> Value {
    json!({
        "name": item.ident.to_string(),
        "type": type_spec(&item.ty),
        "value_expr": tokens(&item.expr),
        "array": array_info(&item.ty, Some(&item.expr)),
        "line": line(item.ident.span()),
        "scope": scope,
        "active": active,
        "cfg": cfg_raw,
    })
}

fn static_fact(item: &ItemStatic, active: &str, cfg_raw: &[String]) -> Value {
    json!({
        "name": item.ident.to_string(),
        "mutable": matches!(item.mutability, syn::StaticMutability::Mut(_)),
        "type": type_spec(&item.ty),
        "value_expr": tokens(&item.expr),
        "array": array_info(&item.ty, Some(&item.expr)),
        "line": line(item.ident.span()),
        "active": active,
        "cfg": cfg_raw,
    })
}

fn has_attr(attrs: &[Attribute], name: &str) -> bool {
    attrs.iter().any(|attr| attr.path().is_ident(name))
}

fn repr_c(attrs: &[Attribute]) -> bool {
    attrs.iter().any(|attr| {
        if !attr.path().is_ident("repr") {
            return false;
        }
        let parsed = attr.parse_args_with(Punctuated::<Meta, syn::Token![,]>::parse_terminated);
        parsed
            .map(|items| items.iter().any(|meta| meta.path().is_ident("C")))
            .unwrap_or(false)
    })
}

fn function_signature(sig: &syn::Signature) -> Value {
    let abi = sig
        .abi
        .as_ref()
        .and_then(|abi| abi.name.as_ref())
        .map(LitStr::value);
    json!({
        "name": sig.ident.to_string(),
        "abi": abi,
        "unsafe": sig.unsafety.is_some(),
        "params": sig.inputs.iter().filter_map(|input| {
            if let syn::FnArg::Typed(typed) = input {
                let name = match typed.pat.as_ref() {
                    Pat::Ident(ident) => Some(ident.ident.to_string()),
                    _ => None,
                };
                Some(json!({
                    "name": name,
                    "type": type_spec(&typed.ty),
                    "text": tokens(&typed.ty),
                }))
            } else {
                None
            }
        }).collect::<Vec<_>>(),
        "ret": match &sig.output {
            ReturnType::Default => json!({"kind": "void"}),
            ReturnType::Type(_, ty) => type_spec(ty),
        },
        "line": line(sig.ident.span()),
    })
}

struct FunctionCollector<'a> {
    cfg: &'a Cfg,
    function: String,
    active: Truth,
    assignments: Vec<Value>,
    locals: Vec<Value>,
    consts: Vec<Value>,
    calls: Vec<Value>,
    method_calls: Vec<Value>,
    checks: Vec<Value>,
    call_context: &'static str,
    block_context_stack: Vec<Option<&'static str>>,
    block_depth: usize,
    add_uses: Vec<Value>,
    index_uses: Vec<Value>,
    loops: Vec<Value>,
    pointer_derefs: usize,
    pointer_arith: usize,
    unsafe_blocks: usize,
    deref_records: Vec<Value>,
    cast_records: Vec<Value>,
    path_refs: Vec<Value>,
    macro_uses: Vec<Value>,
    unsafe_locations: Vec<Value>,
    alias_writes: Vec<Value>,
    lvalue_access: Option<&'static str>,
}

impl<'ast, 'a> FunctionCollector<'a> {
    fn new(cfg: &'a Cfg, function: String, active: Truth) -> Self {
        Self {
            cfg,
            function,
            active,
            assignments: Vec::new(),
            locals: Vec::new(),
            consts: Vec::new(),
            calls: Vec::new(),
            method_calls: Vec::new(),
            checks: Vec::new(),
            call_context: "unclassified",
            block_context_stack: Vec::new(),
            block_depth: 0,
            add_uses: Vec::new(),
            index_uses: Vec::new(),
            loops: Vec::new(),
            pointer_derefs: 0,
            pointer_arith: 0,
            unsafe_blocks: 0,
            deref_records: Vec::new(),
            cast_records: Vec::new(),
            path_refs: Vec::new(),
            macro_uses: Vec::new(),
            unsafe_locations: Vec::new(),
            alias_writes: Vec::new(),
            lvalue_access: None,
        }
    }

    /// Visit an assignment target.  Only the object that is actually mutated
    /// inherits write/read-write access; indices, pointer bases and pointees
    /// used to locate the object remain reads.
    fn visit_lvalue(&mut self, expr: &'ast Expr, access: &'static str) {
        match expr {
            Expr::Path(_) => {
                let previous = self.lvalue_access;
                self.lvalue_access = Some(access);
                self.visit_expr(expr);
                self.lvalue_access = previous;
            }
            Expr::Paren(paren) => self.visit_lvalue(&paren.expr, access),
            Expr::Group(group) => self.visit_lvalue(&group.expr, access),
            Expr::Unary(unary) if matches!(unary.op, syn::UnOp::Deref(_)) => {
                // The pointee is written, but the pointer value used to locate
                // it is only read.  The aliased pointee identity is not
                // resolved by the syntax-only model.
                if let Expr::Path(path) = unary.expr.as_ref() {
                    let name = last_ident(&path.path);
                    self.alias_writes.push(json!({
                        "pointer": name,
                        "access": access,
                        "line": line(unary.span()),
                        "column": column(unary.span()),
                        "function": self.function,
                        "active": self.active.as_str(),
                    }));
                }
                let previous = self.lvalue_access;
                self.lvalue_access = None;
                self.visit_expr(expr);
                self.lvalue_access = previous;
            }
            Expr::Field(field) => {
                // Assigning to a field mutates the base object.  The tagged
                // access lets the Python layer distinguish direct object
                // mutation from a pointer/index base used to locate it.
                let tagged = match access {
                    "readwrite" => "field_readwrite",
                    "write" => "field_write",
                    other => other,
                };
                self.visit_lvalue(&field.base, tagged);
            }
            Expr::Index(index) => {
                // The indexed object is mutated when it is an array.  The
                // Python inventory refines pointer-typed bases to a read.
                self.index_uses.push(json!({
                    "base": tokens(&index.expr),
                    "index": tokens(&index.index),
                    "line": line(index.bracket_token.span.join()),
                    "function": self.function,
                    "active": self.active.as_str(),
                }));
                let tagged = match access {
                    "readwrite" => "index_readwrite",
                    "write" => "index_write",
                    other => other,
                };
                self.visit_lvalue(&index.expr, tagged);
                let previous = self.lvalue_access;
                self.lvalue_access = None;
                self.visit_expr(&index.index);
                self.lvalue_access = previous;
            }
            _ => {
                let previous = self.lvalue_access;
                self.lvalue_access = None;
                self.visit_expr(expr);
                self.lvalue_access = previous;
            }
        }
    }

    /// Visit an address-of operand.  Taking an address is not a value read.
    fn visit_address(&mut self, expr: &'ast Expr) {
        match expr {
            Expr::Path(_) => {
                let previous = self.lvalue_access;
                self.lvalue_access = Some("address_of");
                self.visit_expr(expr);
                self.lvalue_access = previous;
            }
            Expr::Paren(paren) => self.visit_address(&paren.expr),
            Expr::Group(group) => self.visit_address(&group.expr),
            Expr::Field(field) => self.visit_address(&field.base),
            Expr::Index(index) => {
                self.index_uses.push(json!({
                    "base": tokens(&index.expr),
                    "index": tokens(&index.index),
                    "line": line(index.bracket_token.span.join()),
                    "function": self.function,
                    "active": self.active.as_str(),
                }));
                self.visit_address(&index.expr);
                let previous = self.lvalue_access;
                self.lvalue_access = None;
                self.visit_expr(&index.index);
                self.lvalue_access = previous;
            }
            _ => {
                let previous = self.lvalue_access;
                self.lvalue_access = None;
                self.visit_expr(expr);
                self.lvalue_access = previous;
            }
        }
    }
}

impl<'ast, 'a> Visit<'ast> for FunctionCollector<'a> {
    fn visit_local(&mut self, node: &'ast syn::Local) {
        let (truth, _) = self.cfg.attrs(&node.attrs);
        let old_active = self.active;
        self.active = old_active.and(truth);
        if let Some(fact) = local_fact(node, self.active.as_str()) {
            self.locals.push(fact);
        }
        let previous = self.call_context;
        self.call_context = "stored";
        syn::visit::visit_local(self, node);
        self.call_context = previous;
        self.active = old_active;
    }

    fn visit_block(&mut self, node: &'ast syn::Block) {
        let outer = self.call_context;
        self.block_context_stack.push(if outer == "unclassified" {
            None
        } else {
            Some(outer)
        });
        self.block_depth += 1;
        syn::visit::visit_block(self, node);
        self.block_depth -= 1;
        self.block_context_stack.pop();
    }

    fn visit_stmt(&mut self, node: &'ast syn::Stmt) {
        if let syn::Stmt::Expr(expr, semi) = node {
            let previous = self.call_context;
            self.call_context = if semi.is_some() {
                "discarded"
            } else if let Some(Some(outer)) = self.block_context_stack.last().copied() {
                outer
            } else if self.block_depth <= 1 {
                "function_tail"
            } else {
                "tail"
            };
            self.visit_expr(expr);
            self.call_context = previous;
        } else {
            syn::visit::visit_stmt(self, node);
        }
    }

    fn visit_expr_if(&mut self, node: &'ast syn::ExprIf) {
        let condition_span = node.cond.span();
        self.checks.push(json!({
            "kind": "if",
            "condition": tokens(&node.cond),
            "condition_shape": condition_shape(&node.cond),
            "line": line(condition_span),
            "column": column(condition_span),
            "end_line": condition_span.end().line,
            "end_column": condition_span.end().column,
            "function": self.function,
            "active": self.active.as_str(),
        }));
        let previous = self.call_context;
        self.call_context = "condition_check";
        self.visit_expr(&node.cond);
        self.call_context = "control";
        self.block_context_stack.push(Some("control"));
        self.visit_block(&node.then_branch);
        self.block_context_stack.pop();
        if let Some((_else_token, else_branch)) = &node.else_branch {
            self.visit_expr(else_branch);
        }
        self.call_context = previous;
    }

    fn visit_expr_match(&mut self, node: &'ast syn::ExprMatch) {
        let scrutinee_span = node.expr.span();
        self.checks.push(json!({
            "kind": "match",
            "condition": tokens(&node.expr),
            "condition_shape": json!({
                "kind": "match",
                "operator": Value::Null,
                "left": tokens(&node.expr),
                "right": Value::Null,
                "left_call": expr_is_call(&node.expr),
                "right_call": false,
            }),
            "line": line(scrutinee_span),
            "column": column(scrutinee_span),
            "end_line": scrutinee_span.end().line,
            "end_column": scrutinee_span.end().column,
            "function": self.function,
            "active": self.active.as_str(),
        }));
        let previous = self.call_context;
        self.call_context = "condition_check";
        self.visit_expr(&node.expr);
        self.call_context = "control";
        for arm in &node.arms {
            self.visit_arm(arm);
        }
        self.call_context = previous;
    }

    fn visit_expr_try(&mut self, node: &'ast syn::ExprTry) {
        let previous = self.call_context;
        self.call_context = "propagated_via_question_mark";
        self.visit_expr(&node.expr);
        self.call_context = previous;
    }

    fn visit_expr_return(&mut self, node: &'ast syn::ExprReturn) {
        let previous = self.call_context;
        self.call_context = "returned";
        if let Some(expr) = &node.expr {
            self.visit_expr(expr);
        }
        self.call_context = previous;
    }

    fn visit_expr_block(&mut self, node: &'ast syn::ExprBlock) {
        let (truth, _) = self.cfg.attrs(&node.attrs);
        let old = self.active;
        self.active = old.and(truth);
        let outer = self.call_context;
        self.block_context_stack.push(if outer == "unclassified" {
            None
        } else {
            Some(outer)
        });
        syn::visit::visit_expr_block(self, node);
        self.block_context_stack.pop();
        self.active = old;
    }

    fn visit_item_const(&mut self, node: &'ast ItemConst) {
        let (truth, cfg_raw) = self.cfg.attrs(&node.attrs);
        let active = self.active.and(truth);
        self.consts.push(const_fact(node, Some(&self.function), active.as_str(), &cfg_raw));
        syn::visit::visit_item_const(self, node);
    }

    fn visit_expr_assign(&mut self, node: &'ast syn::ExprAssign) {
        self.assignments.push(json!({
            "lhs": tokens(&node.left),
            "rhs": tokens(&node.right),
            "line": line(node.eq_token.span),
            "column": column(node.eq_token.span),
            "function": self.function,
            "active": self.active.as_str(),
        }));
        self.visit_lvalue(&node.left, "write");
        let previous = self.lvalue_access;
        self.lvalue_access = None;
        let previous_context = self.call_context;
        self.call_context = "assigned";
        self.visit_expr(&node.right);
        self.call_context = previous_context;
        self.lvalue_access = previous;
    }

    fn visit_expr_binary(&mut self, node: &'ast syn::ExprBinary) {
        let compound = matches!(
            node.op,
            syn::BinOp::AddAssign(_)
                | syn::BinOp::SubAssign(_)
                | syn::BinOp::MulAssign(_)
                | syn::BinOp::DivAssign(_)
                | syn::BinOp::RemAssign(_)
                | syn::BinOp::BitXorAssign(_)
                | syn::BinOp::BitAndAssign(_)
                | syn::BinOp::BitOrAssign(_)
                | syn::BinOp::ShlAssign(_)
                | syn::BinOp::ShrAssign(_)
        );
        if compound {
            self.visit_lvalue(&node.left, "readwrite");
            let previous = self.lvalue_access;
            self.lvalue_access = None;
            self.visit_expr(&node.right);
            self.lvalue_access = previous;
        } else {
            syn::visit::visit_expr_binary(self, node);
        }
    }

    fn visit_expr_call(&mut self, node: &'ast syn::ExprCall) {
        if let Expr::Path(path) = node.func.as_ref() {
            self.calls.push(json!({
                "callee": last_ident(&path.path),
                "expression": tokens(node),
                "args": node.args.iter().map(tokens).collect::<Vec<_>>(),
                "line": line(path.path.segments.last().map(|s| s.ident.span()).unwrap_or_else(Span::call_site)),
                "column": column(path.path.segments.last().map(|s| s.ident.span()).unwrap_or_else(Span::call_site)),
                "function": self.function,
                "active": self.active.as_str(),
                "context": self.call_context,
            }));
        }
        let previous = self.call_context;
        self.call_context = "argument";
        syn::visit::visit_expr_call(self, node);
        self.call_context = previous;
    }

    fn visit_expr_method_call(&mut self, node: &'ast syn::ExprMethodCall) {
        let method = node.method.to_string();
        self.method_calls.push(json!({
            "receiver": tokens(&node.receiver),
            "method": method,
            "expression": tokens(node),
            "args": node.args.iter().map(tokens).collect::<Vec<_>>(),
            "line": line(node.method.span()),
            "column": column(node.method.span()),
            "function": self.function,
            "active": self.active.as_str(),
            "context": self.call_context,
        }));
        if method == "add" || method == "offset" || method == "sub" {
            self.pointer_arith += 1;
            self.add_uses.push(json!({
                "method": method,
                "receiver": tokens(&node.receiver),
                "offset": node.args.first().map(tokens),
                "line": line(node.method.span()),
                "column": column(node.method.span()),
                "function": self.function,
                "active": self.active.as_str(),
            }));
        }
        let previous = self.call_context;
        self.call_context = "argument";
        syn::visit::visit_expr_method_call(self, node);
        self.call_context = previous;
    }

    fn visit_expr_index(&mut self, node: &'ast syn::ExprIndex) {
        self.index_uses.push(json!({
            "base": tokens(&node.expr),
            "index": tokens(&node.index),
            "line": line(node.bracket_token.span.join()),
            "function": self.function,
            "active": self.active.as_str(),
        }));
        syn::visit::visit_expr_index(self, node);
    }

    fn visit_expr_unary(&mut self, node: &'ast syn::ExprUnary) {
        if matches!(node.op, syn::UnOp::Deref(_)) {
            self.pointer_derefs += 1;
            self.deref_records.push(json!({
                "operand": tokens(&node.expr),
                "line": line(node.span()),
                "column": column(node.span()),
                "function": self.function,
                "active": self.active.as_str(),
            }));
            syn::visit::visit_expr_unary(self, node);
            return;
        }
        syn::visit::visit_expr_unary(self, node);
    }

    fn visit_expr_reference(&mut self, node: &'ast syn::ExprReference) {
        // Taking a reference is an address-taking operation, not a value read.
        self.visit_address(&node.expr);
    }

    fn visit_expr_cast(&mut self, node: &'ast syn::ExprCast) {
        self.cast_records.push(json!({
            "source": tokens(&node.expr),
            "target_type": type_spec(&node.ty),
            "line": line(node.as_token.span),
            "column": column(node.as_token.span),
            "function": self.function,
            "active": self.active.as_str(),
        }));
        syn::visit::visit_expr_cast(self, node);
    }

    fn visit_expr_path(&mut self, node: &'ast syn::ExprPath) {
        let name = last_ident(&node.path);
        self.path_refs.push(json!({
            "name": name,
            "line": line(node.path.segments.last().map(|s| s.ident.span()).unwrap_or_else(Span::call_site)),
            "column": column(node.path.segments.last().map(|s| s.ident.span()).unwrap_or_else(Span::call_site)),
            "function": self.function,
            "active": self.active.as_str(),
            "access": self.lvalue_access.unwrap_or("read"),
        }));
        syn::visit::visit_expr_path(self, node);
    }

    fn visit_expr_macro(&mut self, node: &'ast syn::ExprMacro) {
        self.macro_uses.push(json!({
            "macro": last_ident(&node.mac.path),
            "tokens": tokens(&node.mac),
            "line": line(node.mac.path.segments.last().map(|s| s.ident.span()).unwrap_or_else(Span::call_site)),
            "column": column(node.mac.path.segments.last().map(|s| s.ident.span()).unwrap_or_else(Span::call_site)),
            "function": self.function,
            "active": self.active.as_str(),
            "resolution": "unexpanded",
        }));
        // Deliberately do not traverse macro body: syn has no expansion.
    }

    fn visit_stmt_macro(&mut self, node: &'ast syn::StmtMacro) {
        self.macro_uses.push(json!({
            "macro": last_ident(&node.mac.path),
            "tokens": tokens(&node.mac),
            "line": line(node.mac.path.segments.last().map(|s| s.ident.span()).unwrap_or_else(Span::call_site)),
            "column": column(node.mac.path.segments.last().map(|s| s.ident.span()).unwrap_or_else(Span::call_site)),
            "function": self.function,
            "active": self.active.as_str(),
            "resolution": "unexpanded",
        }));
        // Deliberately do not traverse macro body: syn has no expansion.
    }

    fn visit_expr_unsafe(&mut self, node: &'ast syn::ExprUnsafe) {
        self.unsafe_blocks += 1;
        let span = node.span();
        self.unsafe_locations.push(json!({
            "line": line(node.unsafe_token.span),
            "column": column(node.unsafe_token.span),
            "start_line": line(span),
            "start_column": column(span),
            "end_line": span.end().line,
            "end_column": span.end().column,
            "function": self.function,
            "active": self.active.as_str(),
        }));
        syn::visit::visit_expr_unsafe(self, node);
    }

    fn visit_expr_for_loop(&mut self, node: &'ast syn::ExprForLoop) {
        let var = match node.pat.as_ref() {
            Pat::Ident(ident) => Some(ident.ident.to_string()),
            _ => None,
        };
        let (start, end, inclusive) = match node.expr.as_ref() {
            Expr::Range(range) => (
                range.start.as_ref().map(tokens),
                range.end.as_ref().map(tokens),
                matches!(range.limits, syn::RangeLimits::Closed(_)),
            ),
            _ => (None, Some(tokens(&node.expr)), false),
        };
        self.loops.push(json!({
            "kind": "for",
            "var": var,
            "start": start,
            "end": end,
            "inclusive": inclusive,
            "cond": Value::Null,
            "line": line(node.for_token.span),
            "function": self.function,
            "active": self.active.as_str(),
        }));
        let previous = self.call_context;
        self.call_context = "argument";
        self.visit_expr(&node.expr);
        self.call_context = "control";
        self.block_context_stack.push(Some("control"));
        self.visit_block(&node.body);
        self.block_context_stack.pop();
        self.call_context = previous;
    }

    fn visit_expr_while(&mut self, node: &'ast syn::ExprWhile) {
        let condition_span = node.cond.span();
        self.checks.push(json!({
            "kind": "while",
            "condition": tokens(&node.cond),
            "condition_shape": condition_shape(&node.cond),
            "line": line(condition_span),
            "column": column(condition_span),
            "end_line": condition_span.end().line,
            "end_column": condition_span.end().column,
            "function": self.function,
            "active": self.active.as_str(),
        }));
        self.loops.push(json!({
            "kind": "while",
            "var": Value::Null,
            "start": Value::Null,
            "end": Value::Null,
            "inclusive": false,
            "cond": tokens(&node.cond),
            "line": line(node.while_token.span),
            "function": self.function,
            "active": self.active.as_str(),
        }));
        let previous = self.call_context;
        self.call_context = "condition_check";
        self.visit_expr(&node.cond);
        self.call_context = "control";
        self.block_context_stack.push(Some("control"));
        self.visit_block(&node.body);
        self.block_context_stack.pop();
        self.call_context = previous;
    }

    fn visit_item_fn(&mut self, _node: &'ast ItemFn) {
        // Nested function items are not part of this bench's style. Ignore them
        // here rather than attributing their calls to the enclosing function.
    }
}

fn extract_function(
    cfg: &Cfg,
    sig: &syn::Signature,
    block: &syn::Block,
    attrs: &[Attribute],
    parent_active: Truth,
    module_path: &[String],
    qualified_name: &str,
) -> Value {
    let mut signature = function_signature(sig);
    let (own_active, cfg_raw) = cfg.attrs(attrs);
    let active = parent_active.and(own_active);
    signature["cfg"] = json!(cfg_raw);
    signature["parent_active"] = json!(parent_active.as_str());
    signature["module_path"] = json!(module_path.join("::"));
    signature["simple_name"] = json!(sig.ident.to_string());
    signature["qualified_name"] = json!(qualified_name);
    signature["name"] = json!(qualified_name);
    signature["active"] = json!(active.as_str());
    let body_span = block.span();
    signature["body_start_line"] = json!(line(body_span));
    signature["body_start_column"] = json!(column(body_span));
    signature["body_end_line"] = json!(body_span.end().line);
    signature["body_end_column"] = json!(body_span.end().column);
    let mut collector = FunctionCollector::new(cfg, qualified_name.to_string(), active);
    collector.visit_block(block);
    signature["assignments"] = json!(collector.assignments);
    signature["locals"] = json!(collector.locals);
    signature["local_consts"] = json!(collector.consts);
    signature["calls"] = json!(collector.calls);
    signature["method_calls"] = json!(collector.method_calls);
    signature["checks"] = json!(collector.checks);
    signature["add_uses"] = json!(collector.add_uses);
    signature["index_uses"] = json!(collector.index_uses);
    signature["loops"] = json!(collector.loops);
    signature["pointer_derefs"] = json!(collector.pointer_derefs);
    signature["pointer_arith_ops"] = json!(collector.pointer_arith);
    signature["unsafe_blocks"] = json!(collector.unsafe_blocks);
    signature["deref_records"] = json!(collector.deref_records);
    signature["cast_records"] = json!(collector.cast_records);
    signature["path_refs"] = json!(collector.path_refs);
    signature["macro_uses"] = json!(collector.macro_uses);
    signature["unsafe_locations"] = json!(collector.unsafe_locations);
    signature["alias_writes"] = json!(collector.alias_writes);
    signature
}

struct Extractor<'a> {
    cfg: &'a Cfg,
    file: String,
    input_files: Vec<PathBuf>,
    foreign_fns: Vec<Value>,
    exports: Vec<Value>,
    callbacks: Vec<Value>,
    functions: Vec<Value>,
    structs: Vec<Value>,
    constants: Vec<Value>,
    static_arrays: Vec<Value>,
    static_muts: Vec<Value>,
    unsafe_impls: Vec<Value>,
    unions: Vec<Value>,
    aliases: Vec<Value>,
    uses: Vec<Value>,
    out_of_line_modules: Vec<Value>,
}

impl<'a> Extractor<'a> {
    fn new(cfg: &'a Cfg, file: String, input_files: Vec<PathBuf>) -> Self {
        Self {
            cfg,
            file,
            input_files,
            foreign_fns: Vec::new(),
            exports: Vec::new(),
            callbacks: Vec::new(),
            functions: Vec::new(),
            structs: Vec::new(),
            constants: Vec::new(),
            static_arrays: Vec::new(),
            static_muts: Vec::new(),
            unsafe_impls: Vec::new(),
            unions: Vec::new(),
            aliases: Vec::new(),
            uses: Vec::new(),
            out_of_line_modules: Vec::new(),
        }
    }

    fn qualified_name(module_path: &[String], name: &str) -> String {
        if module_path.is_empty() {
            name.to_string()
        } else {
            format!("{}::{}", module_path.join("::"), name)
        }
    }

    fn module_resolved_by_configuration(&self, name: &str) -> bool {
        self.input_files.iter().any(|path| {
            if path.file_stem().and_then(|stem| stem.to_str()) == Some(name) {
                return true;
            }
            path.file_name().and_then(|file| file.to_str()) == Some("mod.rs")
                && path
                    .parent()
                    .and_then(|parent| parent.file_name())
                    .and_then(|parent| parent.to_str())
                    == Some(name)
        })
    }

    fn process_items(&mut self, items: &[Item], parent_active: Truth, module_path: &[String]) {
        for item in items {
            match item {
                Item::ForeignMod(foreign) => {
                    let (block_own, block_cfg) = self.cfg.attrs(&foreign.attrs);
                    let block_active = parent_active.and(block_own);
                    let abi = foreign
                        .abi
                        .name
                        .as_ref()
                        .map(LitStr::value)
                        .unwrap_or_else(|| "C".to_string());
                    for foreign_item in &foreign.items {
                        if let ForeignItem::Fn(function) = foreign_item {
                            let (own_active, cfg_raw) = self.cfg.attrs(&function.attrs);
                            let active = block_active.and(own_active);
                            let simple_name = function.sig.ident.to_string();
                            let qualified = Self::qualified_name(module_path, &simple_name);
                            let mut fact = function_signature(&function.sig);
                            fact["file"] = json!(self.file);
                            fact["name"] = json!(qualified);
                            fact["simple_name"] = json!(simple_name);
                            fact["qualified_name"] = json!(qualified);
                            fact["module_path"] = json!(module_path.join("::"));
                            fact["abi_block"] = json!(abi);
                            fact["block_cfg"] = json!(block_cfg);
                            fact["cfg"] = json!(cfg_raw);
                            fact["parent_active"] = json!(block_active.as_str());
                            fact["active"] = json!(active.as_str());
                            fact["variadic"] = json!(function.sig.variadic.is_some());
                            self.foreign_fns.push(fact);
                        }
                    }
                }
                Item::Fn(function) => {
                    let simple_name = function.sig.ident.to_string();
                    let qualified = Self::qualified_name(module_path, &simple_name);
                    let fact = extract_function(
                        self.cfg,
                        &function.sig,
                        &function.block,
                        &function.attrs,
                        parent_active,
                        module_path,
                        &qualified,
                    );
                    let abi_c = function
                        .sig
                        .abi
                        .as_ref()
                        .and_then(|abi| abi.name.as_ref())
                        .map(LitStr::value)
                        .as_deref()
                        == Some("C");
                    let exported = has_attr(&function.attrs, "no_mangle")
                        || has_attr(&function.attrs, "export_name");
                    if abi_c && !exported {
                        let mut callback = fact.clone();
                        callback["file"] = json!(self.file);
                        callback["kind"] = json!("rust_callback");
                        self.callbacks.push(callback);
                    }
                    if abi_c && exported {
                        let mut export = fact.clone();
                        export["file"] = json!(self.file);
                        export["kind"] = json!("c_callable_export");
                        self.exports.push(export);
                    }
                    let mut function_fact = fact;
                    function_fact["file"] = json!(self.file);
                    self.functions.push(function_fact);
                }
                Item::Impl(item_impl) => {
                    let (own_active, impl_cfg) = self.cfg.attrs(&item_impl.attrs);
                    let impl_active = parent_active.and(own_active);
                    if item_impl.unsafety.is_some() {
                        let span = item_impl.span();
                        self.unsafe_impls.push(json!({
                            "file": self.file,
                            "line": line(item_impl.impl_token.span),
                            "column": column(item_impl.impl_token.span),
                            "start_line": line(span),
                            "start_column": column(span),
                            "end_line": span.end().line,
                            "end_column": span.end().column,
                            "self_type": tokens(&item_impl.self_ty),
                            "module_path": module_path.join("::"),
                            "parent_active": parent_active.as_str(),
                            "cfg": impl_cfg,
                            "active": impl_active.as_str(),
                        }));
                    }
                    for impl_item in &item_impl.items {
                        if let syn::ImplItem::Fn(method) = impl_item {
                            let simple_name = method.sig.ident.to_string();
                            let qualified = Self::qualified_name(module_path, &simple_name);
                            let mut function = extract_function(
                                self.cfg,
                                &method.sig,
                                &method.block,
                                &method.attrs,
                                impl_active,
                                module_path,
                                &qualified,
                            );
                            function["file"] = json!(self.file);
                            function["impl_type"] = json!(tokens(&item_impl.self_ty));
                            self.functions.push(function);
                        }
                    }
                }
                Item::Union(item_union) => {
                    let (own_active, cfg_raw) = self.cfg.attrs(&item_union.attrs);
                    let active = parent_active.and(own_active);
                    self.unions.push(json!({
                        "name": item_union.ident.to_string(),
                        "file": self.file,
                        "line": line(item_union.ident.span()),
                        "module_path": module_path.join("::"),
                        "parent_active": parent_active.as_str(),
                        "active": active.as_str(),
                        "cfg": cfg_raw,
                        "fields": item_union.fields.named.iter().map(|field| json!({
                            "name": field.ident.as_ref().map(ToString::to_string),
                            "type": type_spec(&field.ty),
                        })).collect::<Vec<_>>(),
                    }));
                }
                Item::Struct(item_struct) => {
                    let (own_active, cfg_raw) = self.cfg.attrs(&item_struct.attrs);
                    let active = parent_active.and(own_active);
                    let fields: Vec<Value> = item_struct
                        .fields
                        .iter()
                        .map(|field| {
                            json!({
                                "name": field.ident.as_ref().map(ToString::to_string),
                                "type": type_spec(&field.ty),
                                "line": line(field.ty.span()),
                            })
                        })
                        .collect();
                    self.structs.push(json!({
                        "name": item_struct.ident.to_string(),
                        "file": self.file,
                        "line": line(item_struct.ident.span()),
                        "module_path": module_path.join("::"),
                        "parent_active": parent_active.as_str(),
                        "repr_c": repr_c(&item_struct.attrs),
                        "cfg": cfg_raw,
                        "active": active.as_str(),
                        "fields": fields,
                    }));
                }
                Item::Const(item_const) => {
                    let (own_active, cfg_raw) = self.cfg.attrs(&item_const.attrs);
                    let active = parent_active.and(own_active);
                    let mut fact = const_fact(item_const, None, active.as_str(), &cfg_raw);
                    fact["file"] = json!(self.file);
                    fact["module_path"] = json!(module_path.join("::"));
                    fact["parent_active"] = json!(parent_active.as_str());
                    self.constants.push(fact);
                }
                Item::Static(item_static) => {
                    let (own_active, cfg_raw) = self.cfg.attrs(&item_static.attrs);
                    let active = parent_active.and(own_active);
                    let mut fact = static_fact(item_static, active.as_str(), &cfg_raw);
                    fact["file"] = json!(self.file);
                    fact["module_path"] = json!(module_path.join("::"));
                    fact["parent_active"] = json!(parent_active.as_str());
                    if fact["mutable"].as_bool() == Some(true) {
                        self.static_muts.push(json!({
                            "name": item_static.ident.to_string(),
                            "file": self.file,
                            "line": line(item_static.ident.span()),
                            "type": type_spec(&item_static.ty),
                            "module_path": module_path.join("::"),
                            "parent_active": parent_active.as_str(),
                            "active": active.as_str(),
                            "cfg": cfg_raw,
                        }));
                    }
                    if fact["array"].is_object() {
                        self.static_arrays.push(fact);
                    }
                }
                Item::Type(item_type) => {
                    let (own_active, cfg_raw) = self.cfg.attrs(&item_type.attrs);
                    let active = parent_active.and(own_active);
                    self.aliases.push(json!({
                        "name": item_type.ident.to_string(),
                        "type": type_spec(&item_type.ty),
                        "file": self.file,
                        "line": line(item_type.ident.span()),
                        "module_path": module_path.join("::"),
                        "active": active.as_str(),
                        "cfg": cfg_raw,
                    }));
                }
                Item::Use(item_use) => {
                    let (own_active, cfg_raw) = self.cfg.attrs(&item_use.attrs);
                    let active = parent_active.and(own_active);
                    self.uses.push(json!({
                        "tree": tokens(&item_use.tree),
                        "file": self.file,
                        "line": line(item_use.use_token.span),
                        "module_path": module_path.join("::"),
                        "active": active.as_str(),
                        "cfg": cfg_raw,
                    }));
                }
                Item::Mod(module) => {
                    let (own_active, cfg_raw) = self.cfg.attrs(&module.attrs);
                    let active = parent_active.and(own_active);
                    let name = module.ident.to_string();
                    let mut child_path = module_path.to_vec();
                    child_path.push(name.clone());
                    if let Some((_, items)) = &module.content {
                        self.process_items(items, active, &child_path);
                    } else {
                        let resolved = self.module_resolved_by_configuration(&name);
                        self.out_of_line_modules.push(json!({
                            "name": name,
                            "file": self.file,
                            "line": line(module.ident.span()),
                            "column": column(module.ident.span()),
                            "module_path": module_path.join("::"),
                            "qualified_name": Self::qualified_name(module_path, &module.ident.to_string()),
                            "parent_active": parent_active.as_str(),
                            "cfg": cfg_raw,
                            "active": active.as_str(),
                            "resolved_by_configuration": resolved,
                        }));
                    }
                }
                _ => {}
            }
        }
    }
}

fn main() {
    let mut args = env::args().skip(1);
    let mut arch = "arm".to_string();
    let mut os = "none".to_string();
    let mut family = "none".to_string();
    let mut pointer_width = "32".to_string();
    let mut test = false;
    let mut features = HashSet::new();
    let mut files = Vec::<PathBuf>::new();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--target-arch" => arch = args.next().unwrap_or_default(),
            "--target-os" => os = args.next().unwrap_or_default(),
            "--target-family" => family = args.next().unwrap_or_default(),
            "--target-pointer-width" => pointer_width = args.next().unwrap_or_default(),
            "--test" => test = true,
            "--feature" => {
                if let Some(feature) = args.next() {
                    features.insert(feature);
                }
            }
            "--help" | "-h" => {
                eprintln!(
                    "usage: uastw-rust-extract [--target-arch arm] [--target-os none] [--target-family none] [--target-pointer-width 32] [--feature NAME] FILE..."
                );
                return;
            }
            other => files.push(PathBuf::from(other)),
        }
    }
    if files.is_empty() {
        eprintln!("uastw-rust-extract: no input files");
        std::process::exit(2);
    }
    let cfg = Cfg {
        arch,
        os,
        family,
        pointer_width,
        test,
        features,
    };

    let mut output_files = Vec::new();
    let mut foreign_fns = Vec::<Value>::new();
    let mut exports = Vec::<Value>::new();
    let mut callbacks = Vec::<Value>::new();
    let mut functions = Vec::<Value>::new();
    let mut structs = Vec::<Value>::new();
    let mut constants = Vec::<Value>::new();
    let mut static_arrays = Vec::<Value>::new();
    let mut static_muts = Vec::<Value>::new();
    let mut unsafe_impls = Vec::<Value>::new();
    let mut unions = Vec::<Value>::new();
    let mut aliases = Vec::<Value>::new();
    let mut uses = Vec::<Value>::new();
    let mut parse_failures = Vec::<Value>::new();
    let mut out_of_line_modules = Vec::<Value>::new();
    let input_files = files.clone();

    for path in files {
        let source = match fs::read_to_string(&path) {
            Ok(source) => source,
            Err(error) => {
                parse_failures.push(json!({
                    "file": path.display().to_string(),
                    "error": error.to_string(),
                }));
                continue;
            }
        };
        let parsed = syn::parse_file(&source);
        let file = match parsed {
            Ok(file) => file,
            Err(error) => {
                parse_failures.push(json!({
                    "file": path.display().to_string(),
                    "error": error.to_string(),
                    "line": error.span().start().line,
                }));
                continue;
            }
        };
        output_files.push(json!({
            "path": path.display().to_string(),
            "bytes": source.len(),
        }));

        let mut extractor = Extractor::new(&cfg, path.display().to_string(), input_files.clone());
        extractor.process_items(&file.items, Truth::True, &[]);
        foreign_fns.append(&mut extractor.foreign_fns);
        exports.append(&mut extractor.exports);
        callbacks.append(&mut extractor.callbacks);
        functions.append(&mut extractor.functions);
        structs.append(&mut extractor.structs);
        constants.append(&mut extractor.constants);
        static_arrays.append(&mut extractor.static_arrays);
        static_muts.append(&mut extractor.static_muts);
        unsafe_impls.append(&mut extractor.unsafe_impls);
        unions.append(&mut extractor.unions);
        aliases.append(&mut extractor.aliases);
        uses.append(&mut extractor.uses);
        out_of_line_modules.append(&mut extractor.out_of_line_modules);
    }

    foreign_fns.sort_by(|a, b| {
        (a["file"].as_str(), a["line"].as_u64(), a["name"].as_str()).cmp(&(
            b["file"].as_str(),
            b["line"].as_u64(),
            b["name"].as_str(),
        ))
    });
    functions.sort_by(|a, b| {
        (a["file"].as_str(), a["line"].as_u64(), a["name"].as_str()).cmp(&(
            b["file"].as_str(),
            b["line"].as_u64(),
            b["name"].as_str(),
        ))
    });
    constants.sort_by(|a, b| {
        (a["file"].as_str(), a["name"].as_str()).cmp(&(b["file"].as_str(), b["name"].as_str()))
    });
    static_arrays.sort_by(|a, b| {
        (a["file"].as_str(), a["name"].as_str()).cmp(&(b["file"].as_str(), b["name"].as_str()))
    });

    let document = json!({
        "schema": "uastw-safety-rust-facts-v1",
        "target": {
            "arch": cfg.arch,
            "os": cfg.os,
            "family": cfg.family,
            "pointer_width": cfg.pointer_width,
            "cfg_evaluated": "simple predicates and Cargo features; unknown predicates are retained",
        },
        "files": output_files,
        "parse_failures": parse_failures,
        "foreign_fns": foreign_fns,
        "exports": exports,
        "callbacks": callbacks,
        "functions": functions,
        "structs": structs,
        "constants": constants,
        "static_arrays": static_arrays,
        "static_muts": static_muts,
        "unsafe_impls": unsafe_impls,
        "unions": unions,
        "type_aliases": aliases,
        "uses": uses,
        "out_of_line_modules": out_of_line_modules,
    });
    println!("{}", serde_json::to_string_pretty(&document).unwrap());
}
