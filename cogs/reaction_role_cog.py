from discord import app_commands
from discord.ext import commands
import re
import discord
from log import setupLogging


# 1. The Dynamic Item Listener
# This catches ANY button click where the custom_id starts with "dynamic_role_"
class DynamicRoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r'dynamic_role_(?P<role_id>[0-9]+)'):
    def __init__(self, role_id: int):
        super().__init__(
            discord.ui.Button(
                custom_id=f"dynamic_role_{role_id}"
            )
        )
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: re.Match[str], /):
        role_id = int(match['role_id'])
        return cls(role_id)

    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)

        if not role:
            await interaction.response.send_message("This role no longer exists!", ephemeral=True)
            return

        try:
            # Toggle the role
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role)
                await interaction.response.send_message(f"*Removed the **{role.name}** role from your profile!*", ephemeral=True)
            else:
                await interaction.user.add_roles(role)
                await interaction.response.send_message(f"*Added the **{role.name}** role to your profile!*", ephemeral=True)

        except discord.Forbidden:
            # This triggers if the bot lacks "Manage Roles" OR if the role it is trying to assign
            # is higher than its own highest role in the server hierarchy.
            await interaction.response.send_message(
                f"❌ I don't have permission to assign or remove the **{role.name}** role.\n\n"
                "*Admins: Please make sure my bot role is physically placed higher than this role in your Server Settings!*",
                ephemeral=True
            )
        except discord.HTTPException:
            # A catch-all for other rare API errors (e.g., Discord outages)
            await interaction.response.send_message(
                "An unexpected error occurred while communicating with Discord. Please try again later.",
                ephemeral=True
            )


class ColorPickerView(discord.ui.View):
    def __init__(self, title_text: str, description_text: str, footer_text: str, embed_color: discord.Color, roles: list[discord.Role]):
        super().__init__()
        self.title_text = title_text
        self.description_text = description_text
        self.footer_text = footer_text
        self.embed_color = embed_color
        self.roles = roles
        self.current_index = 0
        self.configured_roles = []  # Stores tuples of (Role, ButtonStyle)

        # Set the text for the very first role
        self.update_status_text()

    def update_status_text(self):
        """Updates the message text to show which role we are currently coloring."""
        current_role = self.roles[self.current_index]
        self.content = f"**Step 3:** What color should the button for **@{current_role.name}** be? *(Role {self.current_index + 1} of {len(self.roles)})*"

    async def process_color_selection(self, interaction: discord.Interaction, style: discord.ButtonStyle):
        """Saves the color and moves to the next role, or finishes if we are done."""
        current_role = self.roles[self.current_index]
        self.configured_roles.append((current_role, style))
        self.current_index += 1

        # If there are still roles left to color, update the message and wait again
        if self.current_index < len(self.roles):
            self.update_status_text()
            await interaction.response.edit_message(content=self.content, view=self)
        else:
            # We finished all roles! Build the final embed and view.
            await self.publish_final_menu(interaction)

    async def publish_final_menu(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title=self.title_text,
            description=self.description_text,
            color=self.embed_color
        )

        embed.set_footer(text=self.footer_text, icon_url=None)

        public_view = discord.ui.View(timeout=None)

        # Build the buttons using the colors the admin just selected
        for role, style in self.configured_roles:
            public_view.add_item(
                discord.ui.Button(
                    label=role.name,
                    style=style,
                    custom_id=f"dynamic_role_{role.id}"
                )
            )

        # Send the final menu to the channel and close out the setup wizard
        await interaction.channel.send(embed=embed, view=public_view)
        await interaction.response.edit_message(content="✅ Role menu created successfully! Colors applied.", view=None)

    # --- The 4 Discord Button Colors ---
    @discord.ui.button(label="Blue (Primary)", style=discord.ButtonStyle.primary)
    async def blue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_color_selection(interaction, discord.ButtonStyle.primary)

    @discord.ui.button(label="Grey (Secondary)", style=discord.ButtonStyle.secondary)
    async def grey_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_color_selection(interaction, discord.ButtonStyle.secondary)

    @discord.ui.button(label="Green (Success)", style=discord.ButtonStyle.success)
    async def green_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_color_selection(interaction, discord.ButtonStyle.success)

    @discord.ui.button(label="Red (Danger)", style=discord.ButtonStyle.danger)
    async def red_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_color_selection(interaction, discord.ButtonStyle.danger)


# --- 2. The Updated Role Selection Dropdown ---
class RoleSelectView(discord.ui.View):
    def __init__(self, title_text: str, description_text: str, footer_text: str, embed_color: discord.Color):
        super().__init__()
        self.title_text = title_text
        self.description_text = description_text
        self.footer_text = footer_text
        self.embed_color = embed_color

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Select the roles to include...", min_values=1,
                       max_values=25)
    async def select_roles(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        # Instead of building the embed immediately, we pass the data to the Color Picker!
        color_view = ColorPickerView(self.title_text, self.description_text, self.footer_text, self.embed_color, select.values)

        # Replace the dropdown menu with the color picker buttons
        await interaction.response.edit_message(content=color_view.content, view=color_view)


# --- 2. The Form (Modal) ---
# This is what pops up immediately when they type /setup_roles
class SetupModal(discord.ui.Modal, title='Create Role Menu'):
    menu_title = discord.ui.TextInput(
        label='Embed Title',
        placeholder='e.g., Choose your roles!'
    )

    menu_description = discord.ui.TextInput(
        label='Embed Description',
        style=discord.TextStyle.long,
        placeholder='e.g., Click the buttons below to get your roles...'
    )

    menu_footer = discord.ui.TextInput (
        label='Embed footer',
        style=discord.TextStyle.short,
        placeholder='e.g., You can press again to remove the role!'
    )

    embed_color = discord.ui.TextInput(
        label='Embed Color (Hex Code)',
        placeholder='e.g., #FF0000 for red',
        default='#5865F2',  # Defaults to Discord Blurple
        max_length=7,
        required=False
    )

    async def on_submit(self, interaction: discord.Interaction):
        # The form was submitted! Now we send them the Role Dropdown privately.
        try:
            parsed_color = discord.Color.from_str(self.embed_color.value or "#5865F2")
        except ValueError:
            parsed_color = discord.Color.blurple()

        view = RoleSelectView(self.menu_title.value, self.menu_description.value, self.menu_footer.value, parsed_color)
        await interaction.response.send_message(
            "Great! Now select up to 25 roles from the dropdown below to create your buttons:",
            view=view,
            ephemeral=True
        )

class ReactionRoleCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = setupLogging(f"{self.__class__.__name__}")

        self.bot.add_dynamic_items(DynamicRoleButton)

    @app_commands.command(name="setup_roles", description="used by admins to setup the reaction roles")
    @commands.has_permissions(manage_roles=True)
    async def setup_roles(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SetupModal())

    # doing something when the cog gets loaded
    async def cog_load(self):
        self.logger.debug(f"{self.__class__.__name__} loaded!")

    # doing something when the cog gets unloaded
    async def cog_unload(self):
        self.logger.debug(f"{self.__class__.__name__} unloaded!")


# usually you’d use cogs in extensions
# you would then define a global async function named 'setup', and it would take 'bot' as its only parameter
async def setup(bot):
    # finally, adding the cog to the bot
    await bot.add_cog(ReactionRoleCog(bot=bot))
