import os
import base64
import logging
import asyncssh
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives import serialization

logger = logging.getLogger("vpn_manager")

VPN_SSH_HOST = os.getenv("VPN_SSH_HOST")
VPN_SSH_USER = os.getenv("VPN_SSH_USER", "root")
VPN_SSH_PASSWORD = os.getenv("VPN_SSH_PASSWORD")

# Public key and endpoint for the WireGuard server
VPN_SERVER_PUB_KEY = os.getenv("VPN_SERVER_PUB_KEY", "STATIC_MOCK_SERVER_PUBLIC_KEY_BASE64=")
VPN_SERVER_ENDPOINT = os.getenv("VPN_SERVER_ENDPOINT", "127.0.0.1:51820")

def generate_wg_keys():
    """
    Generates Curve25519 private and public keys in standard base64 format for WireGuard.
    """
    private_key = x25519.X25519PrivateKey.generate()
    public_key = private_key.public_key()
    
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption()
    )
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )
    
    private_key_b64 = base64.b64encode(private_bytes).decode('utf-8')
    public_key_b64 = base64.b64encode(public_bytes).decode('utf-8')
    return private_key_b64, public_key_b64

async def register_peer_on_server(public_key: str, ip_address: str):
    """
    Connects to the WireGuard server via SSH and adds the client peer dynamically and persistently.
    """
    if not VPN_SSH_HOST:
        logger.warning("VPN_SSH_HOST is not set. Running WireGuard in MOCK mode.")
        return True
        
    try:
        # Connect using SSH
        async with asyncssh.connect(
            VPN_SSH_HOST,
            username=VPN_SSH_USER,
            password=VPN_SSH_PASSWORD,
            known_hosts=None  # Disable host key check for simplicity
        ) as conn:
            # 1. Register peer dynamically
            cmd_add = f"sudo wg set wg0 peer {public_key} allowed-ips {ip_address}/32"
            res_add = await conn.run(cmd_add)
            if res_add.exit_status != 0:
                logger.error(f"Failed to add peer: {res_add.stderr}")
                raise Exception(f"wg set error: {res_add.stderr}")
                
            # 2. Append to configuration file for persistence after reboot
            cmd_persist = (
                f"grep -q '{public_key}' /etc/wireguard/wg0.conf || "
                f"echo -e '\\n[Peer]\\nPublicKey = {public_key}\\nAllowedIPs = {ip_address}/32' | sudo tee -a /etc/wireguard/wg0.conf"
            )
            res_persist = await conn.run(cmd_persist)
            if res_persist.exit_status != 0:
                logger.warning(f"Failed to make peer persistent in /etc/wireguard/wg0.conf: {res_persist.stderr}")
                
            logger.info(f"Successfully registered peer {public_key} with IP {ip_address} on WG server.")
            return True
    except Exception as e:
        logger.error(f"Failed to connect to VPN server via SSH: {e}")
        raise e

async def generate_user_vpn_config(user_db_id: int) -> str:
    """
    Allocates a unique IP address based on user's database ID and returns the complete WireGuard configuration.
    """
    # IP Allocation schema: starts at 10.8.0.2, holds 250 IPs per subnet block.
    octet_3 = (user_db_id % 250) + 2
    octet_2 = (user_db_id // 250)
    client_ip = f"10.8.{octet_2}.{octet_3}"
    
    # Generate keys
    priv_key, pub_key = generate_wg_keys()
    
    # Register peer on VPN server
    await register_peer_on_server(pub_key, client_ip)
    
    # Generate .conf file
    config_tmpl = (
        "[Interface]\n"
        f"PrivateKey = {priv_key}\n"
        f"Address = {client_ip}/24\n"
        "DNS = 1.1.1.1, 8.8.8.8\n\n"
        "[Peer]\n"
        f"PublicKey = {VPN_SERVER_PUB_KEY}\n"
        f"Endpoint = {VPN_SERVER_ENDPOINT}\n"
        "AllowedIPs = 0.0.0.0/0\n"
        "PersistentKeepalive = 20\n"
    )
    return config_tmpl
