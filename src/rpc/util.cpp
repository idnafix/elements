// Copyright (c) 2017-2018 The Bitcoin Core developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#include <key_io.h>
#include <keystore.h>
#include <rpc/protocol.h>
#include <rpc/util.h>
#include <tinyformat.h>
#include <util/strencodings.h>
#include <policy/policy.h>
#include <assetsdir.h>
#include <core_io.h>

InitInterfaces* g_rpc_interfaces = nullptr;

// Converts a hex string to a public key if possible
CPubKey HexToPubKey(const std::string& hex_in)
{
    if (!IsHex(hex_in)) {
        throw JSONRPCError(RPC_INVALID_ADDRESS_OR_KEY, "Invalid public key: " + hex_in);
    }
    CPubKey vchPubKey(ParseHex(hex_in));
    if (!vchPubKey.IsFullyValid()) {
        throw JSONRPCError(RPC_INVALID_ADDRESS_OR_KEY, "Invalid public key: " + hex_in);
    }
    return vchPubKey;
}

// Retrieves a public key for an address from the given CKeyStore
CPubKey AddrToPubKey(CKeyStore* const keystore, const std::string& addr_in)
{
    CTxDestination dest = DecodeDestination(addr_in);
    if (!IsValidDestination(dest)) {
        throw JSONRPCError(RPC_INVALID_ADDRESS_OR_KEY, "Invalid address: " + addr_in);
    }
    CKeyID key = GetKeyForDestination(*keystore, dest);
    if (key.IsNull()) {
        throw JSONRPCError(RPC_INVALID_ADDRESS_OR_KEY, strprintf("%s does not refer to a key", addr_in));
    }
    CPubKey vchPubKey;
    if (!keystore->GetPubKey(key, vchPubKey)) {
        throw JSONRPCError(RPC_INVALID_ADDRESS_OR_KEY, strprintf("no full public key for address %s", addr_in));
    }
    if (!vchPubKey.IsFullyValid()) {
       throw JSONRPCError(RPC_INTERNAL_ERROR, "Wallet contains an invalid public key");
    }
    return vchPubKey;
}

// Creates a multisig redeemscript from a given list of public keys and number required.
CScript CreateMultisigRedeemscript(const int required, const std::vector<CPubKey>& pubkeys)
{
    // Gather public keys
    if (required < 1) {
        throw JSONRPCError(RPC_INVALID_PARAMETER, "a multisignature address must require at least one key to redeem");
    }
    if ((int)pubkeys.size() < required) {
        throw JSONRPCError(RPC_INVALID_PARAMETER, strprintf("not enough keys supplied (got %u keys, but need at least %d to redeem)", pubkeys.size(), required));
    }
    if (pubkeys.size() > 16) {
        throw JSONRPCError(RPC_INVALID_PARAMETER, "Number of keys involved in the multisignature address creation > 16\nReduce the number");
    }

    CScript result = GetScriptForMultisig(required, pubkeys);

    if (result.size() > MAX_SCRIPT_ELEMENT_SIZE) {
        throw JSONRPCError(RPC_INVALID_PARAMETER, (strprintf("redeemScript exceeds size limit: %d > %d", result.size(), MAX_SCRIPT_ELEMENT_SIZE)));
    }

    return result;
}

class DescribeAddressVisitor : public boost::static_visitor<UniValue>
{
public:
    explicit DescribeAddressVisitor() {}

    UniValue operator()(const CNoDestination& dest) const
    {
        return UniValue(UniValue::VOBJ);
    }

    UniValue operator()(const PKHash& keyID) const
    {
        UniValue obj(UniValue::VOBJ);
        obj.pushKV("isscript", false);
        obj.pushKV("iswitness", false);
        return obj;
    }

    UniValue operator()(const ScriptHash& scriptID) const
    {
        UniValue obj(UniValue::VOBJ);
        obj.pushKV("isscript", true);
        obj.pushKV("iswitness", false);
        return obj;
    }

    UniValue operator()(const WitnessV0KeyHash& id) const
    {
        UniValue obj(UniValue::VOBJ);
        obj.pushKV("isscript", false);
        obj.pushKV("iswitness", true);
        obj.pushKV("witness_version", 0);
        obj.pushKV("witness_program", HexStr(id.begin(), id.end()));
        return obj;
    }

    UniValue operator()(const WitnessV0ScriptHash& id) const
    {
        UniValue obj(UniValue::VOBJ);
        obj.pushKV("isscript", true);
        obj.pushKV("iswitness", true);
        obj.pushKV("witness_version", 0);
        obj.pushKV("witness_program", HexStr(id.begin(), id.end()));
        return obj;
    }

    UniValue operator()(const WitnessUnknown& id) const
    {
        UniValue obj(UniValue::VOBJ);
        obj.pushKV("iswitness", true);
        obj.pushKV("witness_version", (int)id.version);
        obj.pushKV("witness_program", HexStr(id.program, id.program + id.length));
        return obj;
    }

    UniValue operator()(const NullData& id) const
    {
        UniValue obj(UniValue::VOBJ);
        obj.pushKV("isscript", false);
        obj.pushKV("iswitness", false);
        return obj;
    }
};

UniValue DescribeAddress(const CTxDestination& dest)
{
    return boost::apply_visitor(DescribeAddressVisitor(), dest);
}

std::string RPCHelpMan::ToString() const
{
    std::string ret;

    ret += m_name;
    bool is_optional{false};
    for (const auto& arg : m_args) {
        ret += " ";
        if (arg.m_optional) {
            if (!is_optional) ret += "( ";
            is_optional = true;
        } else {
            // Currently we still support unnamed arguments, so any argument following an optional argument must also be optional
            // If support for positional arguments is deprecated in the future, remove this line
            assert(!is_optional);
        }
        ret += arg.ToString();
    }
    if (is_optional) ret += " )";
    ret += "\n";

    return ret;
}

std::string RPCArg::ToStringObj() const
{
    std::string res = "\"" + m_name + "\":";
    switch (m_type) {
    case Type::STR:
        return res + "\"str\"";
    case Type::STR_HEX:
        return res + "\"hex\"";
    case Type::NUM:
        return res + "n";
    case Type::AMOUNT:
        return res + "amount";
    case Type::BOOL:
        return res + "bool";
    case Type::ARR:
        res += "[";
        for (const auto& i : m_inner) {
            res += i.ToString() + ",";
        }
        return res + "...]";
    case Type::OBJ:
    case Type::OBJ_USER_KEYS:
        // Currently unused, so avoid writing dead code
        assert(false);

        // no default case, so the compiler can warn about missing cases
    }
    assert(false);
}

std::string RPCArg::ToString() const
{
    switch (m_type) {
    case Type::STR_HEX:
    case Type::STR: {
        return "\"" + m_name + "\"";
    }
    case Type::NUM:
    case Type::AMOUNT:
    case Type::BOOL: {
        return m_name;
    }
    case Type::OBJ:
    case Type::OBJ_USER_KEYS: {
        std::string res;
        for (size_t i = 0; i < m_inner.size();) {
            res += m_inner[i].ToStringObj();
            if (++i < m_inner.size()) res += ",";
        }
        if (m_type == Type::OBJ) {
            return "{" + res + "}";
        } else {
            return "{" + res + ",...}";
        }
    }
    case Type::ARR: {
        std::string res;
        for (const auto& i : m_inner) {
            res += i.ToString() + ",";
        }
        return "[" + res + "...]";
    }

        // no default case, so the compiler can warn about missing cases
    }
    assert(false);
}

//
// ELEMENTS

class BlindingPubkeyVisitor : public boost::static_visitor<CPubKey>
{
public:
    explicit BlindingPubkeyVisitor() {}

    CPubKey operator()(const CNoDestination& dest) const
    {
        return CPubKey();
    }

    CPubKey operator()(const PKHash& keyID) const
    {
        return keyID.blinding_pubkey;
    }

    CPubKey operator()(const ScriptHash& scriptID) const
    {
        return scriptID.blinding_pubkey;
    }

    CPubKey operator()(const WitnessV0KeyHash& id) const
    {
        return id.blinding_pubkey;
    }

    CPubKey operator()(const WitnessV0ScriptHash& id) const
    {
        return id.blinding_pubkey;
    }

    CPubKey operator()(const WitnessUnknown& id) const
    {
        return id.blinding_pubkey;
    }

    CPubKey operator()(const NullData& id) const
    {
        return CPubKey();
    }
};

CPubKey GetDestinationBlindingKey(const CTxDestination& dest) {
    return boost::apply_visitor(BlindingPubkeyVisitor(), dest);
}

bool IsBlindDestination(const CTxDestination& dest) {
    return GetDestinationBlindingKey(dest).IsFullyValid();
}

class DescribeBlindAddressVisitor : public boost::static_visitor<UniValue>
{
public:

    explicit DescribeBlindAddressVisitor() {}

    UniValue operator()(const CNoDestination& dest) const { return UniValue(UniValue::VOBJ); }

    UniValue operator()(const PKHash& pkhash) const
    {
        UniValue obj(UniValue::VOBJ);
        const CPubKey& blind_pub = pkhash.blinding_pubkey;
        if (IsBlindDestination(pkhash)) {
            obj.pushKV("confidential_key", HexStr(blind_pub.begin(), blind_pub.end()));
            PKHash unblinded(pkhash);
            unblinded.blinding_pubkey = CPubKey();
            obj.pushKV("unconfidential", EncodeDestination(unblinded));
        } else {
            obj.pushKV("confidential_key", "");
            obj.pushKV("unconfidential", EncodeDestination(pkhash));
        }
        return obj;
    }

    UniValue operator()(const ScriptHash& scripthash) const
    {
        UniValue obj(UniValue::VOBJ);
        const CPubKey& blind_pub = scripthash.blinding_pubkey;
        if (IsBlindDestination(scripthash)) {
            obj.pushKV("confidential_key", HexStr(blind_pub.begin(), blind_pub.end()));
            ScriptHash unblinded(scripthash);
            unblinded.blinding_pubkey = CPubKey();
            obj.pushKV("unconfidential", EncodeDestination(unblinded));
        } else {
            obj.pushKV("confidential_key", "");
            obj.pushKV("unconfidential", EncodeDestination(scripthash));
        }

        return obj;
    }

    UniValue operator()(const WitnessV0KeyHash& id) const
    {
        UniValue obj(UniValue::VOBJ);
        const CPubKey& blind_pub = id.blinding_pubkey;
        if (IsBlindDestination(id)) {
            obj.pushKV("confidential_key", HexStr(blind_pub.begin(), blind_pub.end()));
            WitnessV0KeyHash unblinded(id);
            unblinded.blinding_pubkey = CPubKey();
            obj.pushKV("unconfidential", EncodeDestination(unblinded));
        } else {
            obj.pushKV("confidential_key", "");
            obj.pushKV("unconfidential", EncodeDestination(id));
        }

        return obj;
    }

    UniValue operator()(const WitnessV0ScriptHash& id) const
    {
        UniValue obj(UniValue::VOBJ);
        const CPubKey& blind_pub = id.blinding_pubkey;
        if (IsBlindDestination(id)) {
            obj.pushKV("confidential_key", HexStr(blind_pub.begin(), blind_pub.end()));
            WitnessV0ScriptHash unblinded(id);
            unblinded.blinding_pubkey = CPubKey();
            obj.pushKV("unconfidential", EncodeDestination(unblinded));
        } else {
            obj.pushKV("confidential_key", "");
            obj.pushKV("unconfidential", EncodeDestination(id));
        }
        return obj;
    }

    UniValue operator()(const WitnessUnknown& id) const { return UniValue(UniValue::VOBJ); }
    UniValue operator()(const NullData& id) const { return NullUniValue; }
};

UniValue DescribeBlindAddress(const CTxDestination& dest)
{
    UniValue ret(UniValue::VOBJ);
    ret.pushKVs(boost::apply_visitor(DescribeBlindAddressVisitor(), dest));
    return ret;
}

// Attaches labeled balance reports to UniValue obj with asset filter
// "" displays *all* assets as VOBJ pairs, while named assets must have
// been entered via -assetdir configuration argument and are returned as VNUM.
UniValue AmountMapToUniv(const CAmountMap& balanceOrig, std::string strasset)
{
    // Make sure the policyAsset is always present in the balance map.
    CAmountMap balance = balanceOrig;
    balance[::policyAsset] += 0;

    // If we don't do assets or a specific asset is given, we filter out once asset.
    if (!g_con_elementsmode || strasset != "") {
        if (g_con_elementsmode) {
            return ValueFromAmount(balance[GetAssetFromString(strasset)]);
        } else {
            return ValueFromAmount(balance[::policyAsset]);
        }
    }

    UniValue obj(UniValue::VOBJ);
    for(std::map<CAsset, CAmount>::const_iterator it = balance.begin(); it != balance.end(); ++it) {
        // Unknown assets
        if (it->first.IsNull())
            continue;
        UniValue pair(UniValue::VOBJ);
        std::string label = gAssetsDir.GetLabel(it->first);
        if (label == "") {
            label = it->first.GetHex();
        }
        obj.pushKV(label, ValueFromAmount(it->second));
    }
    return obj;
}

// END ELEMENTS
//
