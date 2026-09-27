import re

with open('cogs/admin.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Remove the setup function
code = re.sub(r'async def setup[\s\S]*', '', code)

# Clean up trailing spaces/newlines
code = code.rstrip() + '\n\n'

new_classes = '''
class ImportAuthModal(discord.ui.Modal):
    def __init__(self, domain: str):
        super().__init__(title="🔐 استيراد Cookies / Tokens")
        self.domain_input = discord.ui.TextInput(
            label="الدومين",
            style=discord.TextStyle.short,
            placeholder="asurascans.com",
            required=True,
            max_length=120,
            default=domain,
        )
        self.cookies_input = discord.ui.TextInput(
            label="Raw Cookie Header أو JSON",
            style=discord.TextStyle.paragraph,
            placeholder="cf_clearance=...; access_token=...; refresh_token=...",
            required=True,
            max_length=4000,
        )
        self.user_agent_input = discord.ui.TextInput(
            label="User-Agent (مهم لبعض المواقع مثل Asura)",
            style=discord.TextStyle.paragraph,
            placeholder="Mozilla/5.0 ... (اختياري لكن يفضل وضعه لتجنب الحظر)",
            required=False,
            max_length=1000,
        )
        self.mode_input = discord.ui.TextInput(
            label="الطريقة (merge/replace)",
            style=discord.TextStyle.short,
            placeholder="merge",
            required=False,
            max_length=10,
            default="merge",
        )
        self.add_item(self.domain_input)
        self.add_item(self.cookies_input)
        self.add_item(self.user_agent_input)
        self.add_item(self.mode_input)

    async def on_submit(self, interaction: discord.Interaction):
        domain = str(self.domain_input.value).lower().replace("https://", "").replace("http://", "").split("/")[0]
        data = parse_cookie_string(str(self.cookies_input.value))
        
        # Save user-agent if provided
        ua = str(self.user_agent_input.value).strip()
        if ua:
            data['__custom_user_agent'] = ua
            
        if not data:
            return await interaction.response.send_message("❌ ما لقيت بيانات صالحة في المدخلات.", ephemeral=True)

        mode = str(self.mode_input.value or "").strip().lower()
        if mode not in ("merge", "replace", ""):
            return await interaction.response.send_message("❌ mode لازم يكون merge أو replace.", ephemeral=True)

        if mode == "replace":
            await database.set_site_auth(domain, data)
        else:
            current = await database.get_site_auth(domain) or {}
            current.update(data)
            await database.set_site_auth(domain, current)

        await _reload_and_sync(interaction)
        await interaction.response.send_message(
            f"✅ تم حفظ بيانات `{domain}` (عدد المفاتيح: {len(data)}) ومزامنتها بنجاح.",
            ephemeral=True,
        )

class RemoveKeyModal(discord.ui.Modal):
    def __init__(self, domain: str):
        super().__init__(title="🗑️ حذف Cookie/Token")
        self.domain_input = discord.ui.TextInput(
            label="الدومين",
            style=discord.TextStyle.short,
            placeholder="asurascans.com",
            required=True,
            max_length=120,
            default=domain,
        )
        self.key_input = discord.ui.TextInput(
            label="اسم المفتاح (Cookie name)",
            style=discord.TextStyle.short,
            placeholder="cf_clearance",
            required=True,
            max_length=64,
        )
        self.add_item(self.domain_input)
        self.add_item(self.key_input)

    async def on_submit(self, interaction: discord.Interaction):
        domain = str(self.domain_input.value).lower().replace("https://", "").replace("http://", "").split("/")[0]
        key = str(self.key_input.value).strip()
        auth = await database.get_site_auth(domain) or {}
        if key not in auth:
            return await interaction.response.send_message("❌ المفتاح غير موجود.", ephemeral=True)
        auth.pop(key, None)
        if auth:
            await database.set_site_auth(domain, auth)
        else:
            await database.remove_site_auth(domain)
        await _reload_and_sync(interaction)
        await interaction.response.send_message(f"✅ تم حذف `{key}` من `{domain}` ومزامنة التغيير.", ephemeral=True)

class ClearDomainModal(discord.ui.Modal):
    def __init__(self, domain: str):
        super().__init__(title="⚠️ مسح كل بيانات دومين")
        self.domain_input = discord.ui.TextInput(
            label="الدومين",
            style=discord.TextStyle.short,
            placeholder="asurascans.com",
            required=True,
            max_length=120,
            default=domain,
        )
        self.confirm = discord.ui.TextInput(
            label="اكتب YES للتأكيد",
            style=discord.TextStyle.short,
            placeholder="YES",
            required=True,
            max_length=10,
        )
        self.add_item(self.domain_input)
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction):
        if str(self.confirm.value).strip().upper() != "YES":
            return await interaction.response.send_message("❌ تم الإلغاء (لازم تكتب YES).", ephemeral=True)
        domain = str(self.domain_input.value).lower().replace("https://", "").replace("http://", "").split("/")[0]
        await database.remove_site_auth(domain)
        await _reload_and_sync(interaction)
        await interaction.response.send_message(f"🗑️ تم مسح كل بيانات `{domain}` ومزامنتها.", ephemeral=True)


class DomainSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(placeholder="اختر دومين من القائمة", min_values=1, max_values=1, options=options[:25])

    async def callback(self, interaction: discord.Interaction) -> None:
        view: AuthPanelView = self.view  # type: ignore
        view.selected_domain = self.values[0]
        await interaction.response.send_message(f"✅ تم اختيار `{view.selected_domain}`. يمكنك الآن استخدام الأزرار أدناه.", ephemeral=True)


class AuthPanelView(discord.ui.View):
    def __init__(self, domain_options: list[discord.SelectOption], default_domain: str = "asurascans.com"):
        super().__init__(timeout=600)
        self.selected_domain = default_domain
        self.add_item(DomainSelect(domain_options))

    def _domain(self) -> str:
        return (self.selected_domain or "asurascans.com").lower()

    @discord.ui.button(label="📥 إضافة / استيراد كوكيز", style=discord.ButtonStyle.primary, row=1)
    async def import_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(ImportAuthModal(self._domain()))

    @discord.ui.button(label="🗑️ حذف مفتاح معين", style=discord.ButtonStyle.danger, row=1)
    async def remove_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(RemoveKeyModal(self._domain()))

    @discord.ui.button(label="📜 عرض المفاتيح", style=discord.ButtonStyle.secondary, row=1)
    async def list_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        domain = self._domain()
        auth = await database.get_site_auth(domain) or {}
        keys = list(auth.keys())
        if not keys:
            return await interaction.response.send_message(f"لا توجد مفاتيح محفوظة لـ `{domain}`.", ephemeral=True)
        em = discord.Embed(title=f"🔐 مفاتيح محفوظة — {domain}", color=C_TEAL, timestamp=datetime.datetime.now(datetime.timezone.utc))
        em.description = "\\n".join(f"• `{k}`" for k in keys)[:3900]
        em.set_footer(text="القيم مخفية للأمان")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @discord.ui.button(label="⚠️ مسح كل بيانات الموقع", style=discord.ButtonStyle.danger, row=2)
    async def clear_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(ClearDomainModal(self._domain()))
        
    @discord.ui.button(label="🔎 اختبار (Asura فقط)", style=discord.ButtonStyle.success, row=2)
    async def test_asura_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        domain = self._domain()
        if domain != "asurascans.com":
            return await interaction.response.send_message("❌ هذا الزر مخصص لاختبار AsuraScans فقط.", ephemeral=True)
            
        provider_mgr = getattr(interaction.client, "provider_mgr", None)
        if not provider_mgr:
            return await interaction.response.send_message("❌ Provider manager غير متوفر.", ephemeral=True)
            
        await interaction.response.defer(ephemeral=True)
        chapter_url = "https://asurascans.com/comics/initializing-the-sect-system-7b57f74d/chapter/41"
        try:
            auth = await database.get_site_auth(domain) or {}
            keys_debug = ", ".join(f"{k}" for k in auth.keys()) if auth else "—"
            
            provider = provider_mgr.get_provider(chapter_url)
            html = provider.fetch_html(chapter_url)
            html_len = len(html or "")
            html_hint = ""
            if html:
                if "locked" in html.lower() or "subscribe" in html.lower():
                    html_hint = " (لا زال مقفلاً)"
                    
            imgs = await provider_mgr.get_images(chapter_url)
            if not imgs:
                await interaction.followup.send(f"❌ فشل الاختبار: لم يتم جلب أي صور.\\nالمفاتيح المتاحة: {keys_debug}\\nطول الصفحة: {html_len} {html_hint}")
                return
            
            preview = "\\n".join(f"• {u[:100]}..." for u in imgs[:3])
            await interaction.followup.send(f"✅ تم الاختبار بنجاح! تم العثور على {len(imgs)} صور.\\n{preview}")
        except Exception as e:
            await interaction.followup.send(f"❌ حدث خطأ أثناء الاختبار: {e}")

'''

new_cmd = '''
    @app_commands.command(name="auth_panel", description="[Owner] لوحة تحكم شاملة لإدارة الـ Cookies و الـ User-Agent للمواقع")
    @owner_only()
    async def auth_panel_cmd(self, interaction: discord.Interaction):
        # domains: Asura + custom sites + domains that already have auth
        domains: set[str] = {"asurascans.com"}
        for d, _t, *_ in await database.get_custom_sites():
            if d:
                domains.add(str(d).lower())
        for d, _updated in await database.get_all_site_auth():
            if d:
                domains.add(str(d).lower())

        options = [discord.SelectOption(label=dom, value=dom) for dom in sorted(domains)]
        view = AuthPanelView(options, default_domain="asurascans.com")
        em = discord.Embed(
            title="🔐 لوحة التحكم الشاملة (Auth Panel)",
            description=(
                "اختر الدومين من القائمة أدناه ثم استخدم الأزرار لتحديث الكوكيز.\\n\\n"
                "**💡 مميزات:**\\n"
                "• يمكنك وضع `User-Agent` مخصص للموقع (مهم لـ Asura/Cloudflare).\\n"
                "• يمكنك اختبار تحميل الفصول المقفلة مباشرة من Asura.\\n\\n"
                "⚠️ لا ترسل الكوكيز في الشات العام، استخدم زر **إضافة / استيراد كوكيز** فقط."
            ),
            color=C_INDIGO,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        await interaction.response.send_message(embed=em, view=view, ephemeral=True)
'''

setup_func = '''
async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
'''

code += new_classes
code += new_cmd
code += setup_func

with open('cogs/admin.py', 'w', encoding='utf-8') as f:
    f.write(code)

print("Injected auth_panel into admin.py")
