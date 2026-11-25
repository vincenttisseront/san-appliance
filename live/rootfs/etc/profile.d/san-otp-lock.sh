# Verrouille le mot de passe après la première session interactive.
# Le drapeau appartient à root : seul sudo peut le retirer.
if [ -f /run/san/otp-pending ]; then
    sudo -n passwd -l sanadmin >/dev/null 2>&1 || true
    sudo -n rm -f /run/san/otp-pending || true
fi
