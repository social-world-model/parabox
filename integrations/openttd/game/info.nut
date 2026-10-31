class ParaboxBridgeInfo extends GSInfo {
    function GetAuthor() { return "Parabox"; }
    function GetName() { return "ParaboxBridge"; }
    function GetShortName() { return "PBOX"; }
    function GetDescription() { return "Bounded company actions over the OpenTTD AdminPort."; }
    function GetVersion() { return 1; }
    function MinVersionToLoad() { return 1; }
    function GetDate() { return "2026-10-29"; }
    function CreateInstance() { return "ParaboxBridge"; }
    function GetAPIVersion() { return "15"; }
}

RegisterGS(ParaboxBridgeInfo());
