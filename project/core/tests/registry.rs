use rtos_core::{
    error::Result,
    plugin_interface::{Board, Language, PluginRegistry, Route, RtosPlugin},
};
struct Stub;
impl RtosPlugin for Stub {
    fn id(&self) -> &'static str {
        "stub"
    }
    fn route(&self, _: Board, _: Language) -> Result<Route> {
        unreachable!("registry tests do not select a build route")
    }
}
#[test]
fn duplicate_registration_preserves_original_and_unknown_is_an_error() {
    let mut registry = PluginRegistry::default();
    registry.register(Box::new(Stub)).unwrap();
    assert!(registry.register(Box::new(Stub)).is_err());
    assert_eq!(registry.get("stub").unwrap().id(), "stub");
    assert!(registry.get("missing").is_err());
}
