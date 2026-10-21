//! Small GDB/MI parser: tokenized results, async state records and C strings.
use crate::error::{Error, Result};
use serde_json::{Map, Value};

#[derive(Debug)]
pub(super) struct Record {
    pub token: Option<u64>,
    pub kind: u8,
    pub class: String,
    pub fields: Value,
}

pub(super) fn parse(line: &str) -> Result<Option<Record>> {
    let bytes = line.trim().as_bytes();
    let mut p = Parser {
        bytes,
        at: 0,
        depth: 0,
    };
    while p.peek().is_some_and(|b| b.is_ascii_digit()) {
        p.at += 1;
    }
    let token = std::str::from_utf8(&bytes[..p.at])
        .ok()
        .and_then(|s| s.parse().ok());
    let Some(kind @ (b'^' | b'*' | b'=' | b'+')) = p.peek() else {
        return Ok(None);
    };
    p.at += 1;
    let start = p.at;
    while p.peek().is_some_and(|b| b != b',') {
        p.at += 1;
    }
    let class = String::from_utf8_lossy(&bytes[start..p.at]).to_string();
    let mut fields = Map::new();
    while p.peek() == Some(b',') {
        p.at += 1;
        let (key, value) = p.field()?;
        fields.insert(key, value);
    }
    if p.at != bytes.len() {
        return Err(invalid());
    }
    Ok(Some(Record {
        token,
        kind,
        class,
        fields: Value::Object(fields),
    }))
}

fn invalid() -> Error {
    Error::Protocol("malformed GDB/MI record; see transcript".into())
}

struct Parser<'a> {
    bytes: &'a [u8],
    at: usize,
    depth: usize,
}
impl Parser<'_> {
    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.at).copied()
    }
    fn field(&mut self) -> Result<(String, Value)> {
        let start = self.at;
        while self
            .peek()
            .is_some_and(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        {
            self.at += 1;
        }
        if self.at == start || self.peek() != Some(b'=') {
            return Err(invalid());
        }
        let name = String::from_utf8_lossy(&self.bytes[start..self.at]).to_string();
        self.at += 1;
        Ok((name, self.value()?))
    }
    fn value(&mut self) -> Result<Value> {
        self.depth += 1;
        if self.depth > 32 {
            return Err(invalid());
        }
        let value = match self.peek() {
            Some(b'"') => Value::String(self.string()?),
            Some(b'{') => {
                self.at += 1;
                let mut map = Map::new();
                while self.peek() != Some(b'}') {
                    let (key, value) = self.field()?;
                    map.insert(key, value);
                    if self.peek() == Some(b',') {
                        self.at += 1;
                    } else {
                        break;
                    }
                }
                if self.peek() != Some(b'}') {
                    return Err(invalid());
                }
                self.at += 1;
                Value::Object(map)
            }
            Some(b'[') => {
                self.at += 1;
                let mut list = Vec::new();
                while self.peek() != Some(b']') {
                    if matches!(self.peek(), Some(b'"' | b'{' | b'[')) {
                        list.push(self.value()?);
                    } else {
                        let (key, value) = self.field()?;
                        let mut map = Map::new();
                        map.insert(key, value);
                        list.push(Value::Object(map));
                    }
                    if self.peek() == Some(b',') {
                        self.at += 1;
                    } else {
                        break;
                    }
                }
                if self.peek() != Some(b']') {
                    return Err(invalid());
                }
                self.at += 1;
                Value::Array(list)
            }
            _ => return Err(invalid()),
        };
        self.depth -= 1;
        Ok(value)
    }
    fn string(&mut self) -> Result<String> {
        self.at += 1;
        let mut out = Vec::new();
        loop {
            let c = self.peek().ok_or_else(invalid)?;
            self.at += 1;
            match c {
                b'"' => break,
                b'\\' => {
                    let escaped = self.peek().ok_or_else(invalid)?;
                    self.at += 1;
                    out.push(match escaped {
                        b'n' => b'\n',
                        b'r' => b'\r',
                        b't' => b'\t',
                        b'\\' => b'\\',
                        b'"' => b'"',
                        b'a' => 7,
                        b'b' => 8,
                        b'f' => 12,
                        b'v' => 11,
                        b'0'..=b'7' => {
                            let mut n = u16::from(escaped - b'0');
                            for _ in 0..2 {
                                if let Some(b @ b'0'..=b'7') = self.peek() {
                                    n = n * 8 + u16::from(b - b'0');
                                    self.at += 1;
                                } else {
                                    break;
                                }
                            }
                            u8::try_from(n).map_err(|_| invalid())?
                        }
                        _ => return Err(invalid()),
                    });
                }
                _ => out.push(c),
            }
        }
        Ok(String::from_utf8_lossy(&out).into_owned())
    }
}
