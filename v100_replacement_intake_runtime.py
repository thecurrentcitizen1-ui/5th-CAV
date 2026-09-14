"""Runtime compatibility patch for the V310/V100 Replacement Intake Card.

Battalion Clerk's bot.py is intentionally left intact. The production bootstrap
loads this module, which injects the revised intake UI immediately before the
bot starts. Existing recruiting APIs, slash commands, manual-intake queue, role
handling, and case submission remain authoritative.
"""
from __future__ import annotations


MARKER = "# Consolidated to preserve global slash-command headroom."


INJECTION = r'''
# ---------------------------------------------------------------------------
# V100 / V310 REPLACEMENT INTAKE CARD — WEBSITE/DISCORD QUESTION PARITY
# ---------------------------------------------------------------------------
V310_PRIMARY_MOS = [
    'Rifleman / Infantry','Automatic Rifleman / Machine Gunner','Grenadier','Medic',
    'Combat Engineer','Logistics / Support','Crewman / Armor','Helicopter / Aviation',
    'Leadership — Later','No preference'
]
V310_LEARN_MOS = ['Machine Gunner','Grenadier','Medic','Engineer','Squad Leader','Recon / Sniper','Armor','Helicopter / Aviation','Logistics']
V310_GAME_INTERESTS = ['Infantry','Armor','Aviation','Recon','Mortars','Logistics','Leadership','Seeding','Organized Operations','Public Matches','Training','Community Events']
V310_PLAY_TRAITS = ['Aggressive','Tactical','Support-focused','Objective-focused','Leadership','Flexible']
V310_AVAILABILITY = ['Weekday daytime','Weekday evenings','Weekend daytime','Weekend evenings','Late night','Varies / whenever available']


def _v310_opts(values, *, none_label=None):
    opts=[]
    if none_label:
        opts.append(discord.SelectOption(label=none_label,value='__NONE__'))
    opts.extend(discord.SelectOption(label=x[:100],value=x) for x in values)
    return opts


async def _v310_draft_answers(user):
    gid=_recruit_guild_id(user)
    try:
        status=await web.request('GET','/internal/clerk/recruiting/intake/status',params={'guild_id':gid,'discord_user_id':user.id})
        draft=(status or {}).get('draft') or {}
        answers=draft.get('answers') or {}
        return answers if isinstance(answers,dict) else {}
    except Exception as exc:
        log.warning('[V100 INTAKE DRAFT READ FAILED] user=%s error=%s',getattr(user,'id',None),exc)
        return {}


def _v310_list(value):
    if isinstance(value,list): return [str(x) for x in value if str(x).strip()]
    if value is None: return []
    text=str(value).strip()
    return [text] if text else []


def _v310_summary(a):
    primary=str(a.get('role_interest') or '').strip()
    extras=_v310_list(a.get('preferred_mos_choices'))
    return '\n'.join([
        'V310 REPLACEMENT INTAKE',
        f"Experience: {a.get('experience_style') or a.get('play_style') or 'Not filed'}",
        f"Primary MOS: {primary or 'Not filed'}",
        'Other MOS: '+(', '.join(extras) if extras else 'None'),
        'Wants to learn: '+(', '.join(_v310_list(a.get('learn_mos_choices'))) or 'None'),
        'Interests: '+(', '.join(_v310_list(a.get('game_interest_choices'))) or 'None'),
        'Play traits: '+(', '.join(_v310_list(a.get('play_trait_choices'))) or 'None'),
        'Availability: '+(', '.join(_v310_list(a.get('availability_choices'))) or str(a.get('participation') or 'Not filed')),
        f"Future leadership: {a.get('leadership_interest') or 'Not filed'}",
        f"Looking for: {a.get('looking_for') or 'Not filed'}",
        f"For future Squad Leader: {a.get('future_squad_note') or 'None'}",
    ])[:1900]


class RecruitBasicsModal(discord.ui.Modal, title='Replacement Intake Card — Part 1 of 3'):
    age=discord.ui.TextInput(label='01 — Age',required=True,max_length=2,placeholder='13–99')
    timezone_name=discord.ui.TextInput(label='02 — Time zone',max_length=60,placeholder='Example: Eastern / EDT')
    game_platform=discord.ui.TextInput(label='03 — Platform',max_length=30,placeholder='STEAM, XBOX, or PS5')
    game_identity=discord.ui.TextInput(label='04 — Steam ID / console gamertag',max_length=100,placeholder='SteamID64, Xbox Gamertag, or PSN ID')

    async def on_submit(self, interaction:discord.Interaction):
        platform=RECRUIT_PLATFORM_ALIASES.get(str(self.game_platform.value).strip().upper())
        identity=str(self.game_identity.value).strip()
        age=str(self.age.value).strip()
        if not age.isdigit() or not (13 <= int(age) <= 99):
            await interaction.response.send_message('Age must be a number from **13 to 99**. Press Part 1 and try again.',ephemeral=_recruit_ephemeral(interaction)); return
        if not platform:
            await interaction.response.send_message('Platform must be **STEAM**, **XBOX**, or **PS5**. Press Part 1 and try again.',ephemeral=_recruit_ephemeral(interaction)); return
        if platform=='STEAM' and not (identity.isdigit() and len(identity)==17):
            await interaction.response.send_message('Steam players must enter a **17-digit SteamID64**. Press Part 1 and try again.',ephemeral=_recruit_ephemeral(interaction)); return
        try:
            await _recruit_save(interaction.user,2,{
                'age':age,'timezone_name':str(self.timezone_name.value).strip(),
                'game_platform':platform,'game_identity':identity
            })
            await interaction.response.send_message(
                '**PART 1 FILED.** Part 2 is your Replacement Interest Card — the same preferences shown on the website.',
                view=RecruitPart2View(),ephemeral=_recruit_ephemeral(interaction))
        except Exception as exc:
            await interaction.response.send_message(f'Could not save your Discord intake: {str(exc)[:300]}',ephemeral=_recruit_ephemeral(interaction))


class V310ExperienceSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder='05 — Choose Milsim, Tactical, or Casual',min_values=1,max_values=1,options=[
            discord.SelectOption(label='MILSIM',value='MILSIM',description='Organized ops, role discipline, tactical comms'),
            discord.SelectOption(label='TACTICAL',value='TACTICAL',description='Teamwork and structure without another job'),
            discord.SelectOption(label='CASUAL',value='CASUAL',description='Relaxed jump-in-and-play teamwork'),
        ],custom_id='v310_intake_experience')
    async def callback(self,interaction):
        value=self.values[0]
        await _recruit_save(interaction.user,2,{'experience_style':value,'play_style':value})
        await interaction.response.edit_message(content='**06 — PRIMARY MOS / ROLE**\nPreference only. Your initial assignment can still default to Rifleman.',view=V310PrimaryMosView())

class V310ExperienceView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None); self.add_item(V310ExperienceSelect())


class V310PrimaryMosSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder='06 — Select your primary MOS / role',min_values=1,max_values=1,options=_v310_opts(V310_PRIMARY_MOS),custom_id='v310_intake_primary_mos')
    async def callback(self,interaction):
        await _recruit_save(interaction.user,2,{'role_interest':self.values[0]})
        await interaction.response.edit_message(content='**07 — OTHER MOS INTERESTS**\nOptional — choose up to two more roles you enjoy, or choose NONE.',view=V310OtherMosView())

class V310PrimaryMosView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None); self.add_item(V310PrimaryMosSelect())


class V310OtherMosSelect(discord.ui.Select):
    def __init__(self):
        values=V310_PRIMARY_MOS[:8]
        super().__init__(placeholder='07 — Up to two additional MOS interests',min_values=1,max_values=2,options=_v310_opts(values,none_label='NONE / NO ADDITIONAL MOS'),custom_id='v310_intake_other_mos')
    async def callback(self,interaction):
        values=[x for x in self.values if x!='__NONE__']
        if '__NONE__' in self.values and values:
            await interaction.response.send_message('Choose **NONE** by itself, or choose up to two MOS interests.',ephemeral=True); return
        await _recruit_save(interaction.user,2,{'preferred_mos_choices':values})
        await interaction.response.edit_message(content='**08 — ROLES YOU WANT TO LEARN**\nOptional — select the roles you would like to grow into.',view=V310LearnMosView())

class V310OtherMosView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None); self.add_item(V310OtherMosSelect())


class V310LearnMosSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder='08 — Select roles you want to learn',min_values=1,max_values=9,options=_v310_opts(V310_LEARN_MOS,none_label='NONE / NOT SURE YET'),custom_id='v310_intake_learn_mos')
    async def callback(self,interaction):
        values=[x for x in self.values if x!='__NONE__']
        if '__NONE__' in self.values and values:
            await interaction.response.send_message('Choose **NONE** by itself, or choose the roles you want to learn.',ephemeral=True); return
        await _recruit_save(interaction.user,2,{'learn_mos_choices':values})
        await interaction.response.edit_message(content='**09 — WHAT PARTS OF HLL VIETNAM INTEREST YOU?**\nSelect anything you would like to do with the unit.',view=V310InterestsView())

class V310LearnMosView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None); self.add_item(V310LearnMosSelect())


class V310InterestsSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder='09 — Select battlefield/community interests',min_values=1,max_values=12,options=_v310_opts(V310_GAME_INTERESTS,none_label='NONE / NO PREFERENCE'),custom_id='v310_intake_interests')
    async def callback(self,interaction):
        values=[x for x in self.values if x!='__NONE__']
        if '__NONE__' in self.values and values:
            await interaction.response.send_message('Choose **NONE** by itself, or choose your interests.',ephemeral=True); return
        await _recruit_save(interaction.user,2,{'game_interest_choices':values})
        await interaction.response.edit_message(content='**10 — HOW DO YOU NORMALLY LIKE TO PLAY?**\nChoose as many as fit you.',view=V310TraitsView())

class V310InterestsView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None); self.add_item(V310InterestsSelect())


class V310TraitsSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder='10 — Select your play style traits',min_values=1,max_values=6,options=_v310_opts(V310_PLAY_TRAITS,none_label='NONE / NO PREFERENCE'),custom_id='v310_intake_traits')
    async def callback(self,interaction):
        values=[x for x in self.values if x!='__NONE__']
        if '__NONE__' in self.values and values:
            await interaction.response.send_message('Choose **NONE** by itself, or choose your play-style traits.',ephemeral=True); return
        await _recruit_save(interaction.user,2,{'play_trait_choices':values})
        await interaction.response.edit_message(content='**11 — WHEN ARE YOU USUALLY ACTIVE?**\nThis is **not a mandatory schedule**. It only helps NCOs match you with people usually online at the same time.',view=V310AvailabilityView())

class V310TraitsView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None); self.add_item(V310TraitsSelect())


class V310AvailabilitySelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder='11 — Select your usual availability',min_values=1,max_values=6,options=_v310_opts(V310_AVAILABILITY),custom_id='v310_intake_availability')
    async def callback(self,interaction):
        values=list(self.values)
        await _recruit_save(interaction.user,2,{'availability_choices':values,'participation':' • '.join(values)})
        await interaction.response.edit_message(content='**12 — INTERESTED IN FUTURE LEADERSHIP?**',view=V310LeadershipView())

class V310AvailabilityView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None); self.add_item(V310AvailabilitySelect())


class V310LeadershipSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder='12 — Future leadership interest',min_values=1,max_values=1,options=_v310_opts(['YES','MAYBE LATER','NO']),custom_id='v310_intake_leadership')
    async def callback(self,interaction):
        await _recruit_save(interaction.user,2,{'leadership_interest':self.values[0]})
        await interaction.response.send_modal(V310GoalsModal())

class V310LeadershipView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None); self.add_item(V310LeadershipSelect())


class V310GoalsModal(discord.ui.Modal, title='Replacement Intake Card — Goals'):
    looking_for=discord.ui.TextInput(label='13 — What are you looking for?',style=discord.TextStyle.paragraph,max_length=1000,placeholder='What are you looking for from the 1/5 Cavalry?')
    future_squad_note=discord.ui.TextInput(label='14 — Future Squad Leader note',required=False,style=discord.TextStyle.paragraph,max_length=1000,placeholder='Anything your future Squad Leader should know?')
    async def on_submit(self,interaction):
        try:
            await _recruit_save(interaction.user,3,{
                'looking_for':str(self.looking_for.value).strip(),
                'future_squad_note':str(self.future_squad_note.value).strip(),
            })
            await interaction.response.send_message('**PART 2 FILED.** One final section remains: recruiter attribution and community standards.',view=RecruitPart3View(),ephemeral=_recruit_ephemeral(interaction))
        except Exception as exc:
            await interaction.response.send_message(f'Could not save your Discord intake: {str(exc)[:300]}',ephemeral=_recruit_ephemeral(interaction))


class RecruitFinalDetailsModal(discord.ui.Modal, title='Replacement Intake Card — Final'):
    community_ack=discord.ui.TextInput(label='16 — Community standards — agree?',max_length=12,placeholder='Type YES')
    def __init__(self,recruited_by:str='NONE',recruiter_personnel_id:str|None=None,recruiter_discord_user_id:int|None=None):
        super().__init__(); self.recruited_by=recruited_by; self.recruiter_personnel_id=recruiter_personnel_id; self.recruiter_discord_user_id=recruiter_discord_user_id
    async def on_submit(self,interaction):
        ack=str(self.community_ack.value).strip().upper()
        if ack not in {'YES','Y','AGREE','I AGREE'}:
            await interaction.response.send_message('You must answer **YES** to the community standards acknowledgment to file the intake.',ephemeral=_recruit_ephemeral(interaction)); return
        await interaction.response.defer(thinking=True,ephemeral=_recruit_ephemeral(interaction))
        try:
            prior=await _v310_draft_answers(interaction.user)
            prior.update({
                'recruited_by':self.recruited_by,
                'recruited_by_personnel_id':self.recruiter_personnel_id,
                'recruited_by_discord_user_id':self.recruiter_discord_user_id,
                'community_ack':'YES','follows_chain':'YES','heard_about':'Discord / /apply',
            })
            prior['applicant_notes']=_v310_summary(prior)
            await _recruit_save(interaction.user,4,{
                'recruited_by':self.recruited_by,
                'recruited_by_personnel_id':self.recruiter_personnel_id,
                'recruited_by_discord_user_id':self.recruiter_discord_user_id,
                'community_ack':'YES','follows_chain':'YES','heard_about':'Discord / /apply',
                'applicant_notes':prior['applicant_notes'],
            })
            gid=_recruit_guild_id(interaction.user)
            result=await web.request('POST','/internal/clerk/recruiting/intake/submit',json={'guild_id':gid,'discord_user_id':interaction.user.id})
            case=result.get('case') or {}
            primary=str(prior.get('role_interest') or '').strip()
            extras=[x for x in _v310_list(prior.get('preferred_mos_choices')) if x and x!=primary]
            preferred=[]
            for x in ([primary] if primary else [])+extras:
                if x and x not in preferred: preferred.append(x)
            try:
                await web.request('POST','/internal/clerk/recruiting/intake-profile',json={
                    'discord_user_id':interaction.user.id,
                    'discord_username':getattr(interaction.user,'name',str(interaction.user)),
                    'experience_style':prior.get('experience_style') or prior.get('play_style'),
                    'preferred_mos':preferred[:3],
                    'learn_mos':_v310_list(prior.get('learn_mos_choices')),
                    'game_interests':_v310_list(prior.get('game_interest_choices')),
                    'play_traits':_v310_list(prior.get('play_trait_choices')),
                    'availability':_v310_list(prior.get('availability_choices')),
                    'leadership_interest':prior.get('leadership_interest'),
                    'looking_for':prior.get('looking_for'),
                    'future_squad_note':prior.get('future_squad_note'),
                    'timezone_name':prior.get('timezone_name'),
                    'game_platform':prior.get('game_platform'),
                    'game_identity':prior.get('game_identity'),
                    'recruited_by_personnel_id':self.recruiter_personnel_id,
                    'case_id':case.get('id'),
                    'personnel_id':case.get('personnel_id'),
                })
            except Exception as profile_exc:
                log.warning('[V100 INTAKE PROFILE SIDECAR FAILED] user=%s case=%s error=%s',interaction.user.id,case.get('case_number'),profile_exc)
            if result.get('existing_case'):
                text=f"**RECRUITING CASE ALREADY ON FILE — {case.get('case_number','RECRUITING CASE')}**\nStatus: **{str(case.get('status') or '').replace('_',' ')}**"
            elif result.get('updated_existing_case'):
                text=(f"**REPLACEMENT INTAKE CARD FILED — {case.get('case_number','RECRUITING CASE')}**\nYour Recruiting Case has been updated and returned to Battalion Headquarters for review.\nYou do **not** need to complete another interest form on the website.")
            else:
                text=(f"**1/5 CAV — REPLACEMENT INTAKE CARD FILED**\nRecruiting Case **{case.get('case_number')}** has been forwarded to Battalion Headquarters.\nStatus: **AWAITING COMMAND REVIEW**\n\nThis is the same intake used by the website. You do **not** need to complete it twice.")
                if result.get('status_url'): text+=f"\nCase status: {result['status_url']}"
            if self.recruiter_personnel_id:
                text+=f"\nRecruiter attribution: **FILED FOR {self.recruited_by}** — credit becomes verified when you reach Enlisted status."
            await interaction.followup.send(text,ephemeral=_recruit_ephemeral(interaction))
            try:
                guild=bot.get_guild(gid); member=guild.get_member(interaction.user.id) if guild else None
                if member: await ensure_recruit_status_role(member,approved=False)
            except Exception as exc:
                log.warning('[V100 DISCORD INTAKE ROLE FAILED] user=%s error=%s',interaction.user.id,exc)
        except Exception as exc:
            await interaction.followup.send(f'**REPLACEMENT INTAKE NOT FILED**\n{str(exc)[:500]}\nYour completed sections were saved. Press **Begin / Resume Intake** again to retry.',ephemeral=_recruit_ephemeral(interaction))


class RecruitPart1View(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label='PART 1 — IDENTITY',style=discord.ButtonStyle.primary,custom_id='recruit_apply_part1')
    async def part1(self,interaction,button): await interaction.response.send_modal(RecruitBasicsModal())

class RecruitPart2View(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label='PART 2 — INTEREST CARD',style=discord.ButtonStyle.primary,custom_id='recruit_apply_part2')
    async def part2(self,interaction,button):
        await interaction.response.send_message('**05 — WHAT TYPE OF EXPERIENCE ARE YOU LOOKING FOR?**\nChoose the environment you want most often. You can still play with anyone in the battalion.',view=V310ExperienceView(),ephemeral=_recruit_ephemeral(interaction))

class RecruitPart3View(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label='PART 3 — FINAL & SUBMIT',style=discord.ButtonStyle.success,custom_id='recruit_apply_part3')
    async def part3(self,interaction,button):
        await interaction.response.send_message(
            '**15 — WERE YOU RECRUITED BY AN ACTIVE 1/5 CAV MEMBER?**\nSelect YES and choose the member, or select NO.\n\n'
            '**16 — COMMUNITY STANDARDS**\nI will treat members with respect and follow the community rules. I understand participation and training are voluntary. You will confirm **YES** before filing.',
            view=RecruitReferralView(),ephemeral=_recruit_ephemeral(interaction))


async def _begin_or_resume_recruit_application(interaction:discord.Interaction):
    user=interaction.user; gid=_recruit_guild_id(user)
    if not gid:
        await interaction.response.send_message('The battalion Discord server is unavailable right now. Please try again later.',ephemeral=_recruit_ephemeral(interaction)); return
    try:
        data=await web.request('POST','/internal/clerk/recruiting/intake/start',json={
            'guild_id':gid,'discord_user_id':user.id,'username':getattr(user,'name',str(user)),
            'display_name':getattr(user,'display_name',getattr(user,'name',str(user)))
        })
    except Exception as exc:
        await interaction.response.send_message(f'Replacement Intake is temporarily unavailable: {str(exc)[:300]}',ephemeral=_recruit_ephemeral(interaction)); return
    if data.get('existing_member'):
        await interaction.response.send_message('Your Discord account is already linked to an active 1/5 Cavalry Soldier Record. No recruiting intake is required.',ephemeral=_recruit_ephemeral(interaction)); return
    if data.get('existing_case'):
        case=data.get('case') or {}; url=f"{WEBSITE_BASE_URL}/recruiting/status/{case.get('public_token')}" if WEBSITE_BASE_URL and case.get('public_token') else None
        text=f"**RECRUITING CASE ALREADY ON FILE — {case.get('case_number')}**\nStatus: **{str(case.get('status') or '').replace('_',' ')}**"
        if url: text+=f"\n{url}"
        await interaction.response.send_message(text,ephemeral=_recruit_ephemeral(interaction)); return
    draft=data.get('draft') or {}; step=max(1,min(3,int(draft.get('current_step') or 1)))
    target_case=data.get('target_case') or {}; views={1:RecruitPart1View,2:RecruitPart2View,3:RecruitPart3View}
    case_line=f"\nRecruiting Case: **{target_case.get('case_number')}**" if target_case else ''
    await interaction.response.send_message(
        f"**1/5 CAV — REPLACEMENT CENTER**{case_line}\nYour Replacement Intake Card is {'ready to resume' if draft.get('answers') else 'ready to begin'}. "
        f"Complete Part **{step} of 3** below. Every answer is saved to the same Recruiting Case used by the website.\n\n"
        "There is **no mandatory schedule** and this does not permanently lock you to a role. If you have not linked your game account yet, run **`/link-game`** after the intake.",
        view=views[step](),ephemeral=_recruit_ephemeral(interaction))


def _manual_recruit_intake_message(case_number):
    return (f"**1/5 CAV — REPLACEMENT INTAKE CARD**\nRecruiting Case: **{case_number or 'ON FILE'}**\n\n"
            "Battalion Headquarters needs your Replacement Intake Card. This Discord intake asks the **same questions and choices as the website intake**. Your answers are saved to the same Recruiting Case — do not complete both.\n\n"
            "Use **BEGIN / RESUME INTAKE** below. If you stop, run `/apply` later and continue where you left off.")

log.info('[V100] Discord Replacement Intake questions matched to website V310')
'''


def inject(source: str) -> str:
    if '[V100] Discord Replacement Intake questions matched to website V310' in source:
        return source
    if MARKER not in source:
        raise RuntimeError('V100 injection marker not found in bot.py')
    return source.replace(MARKER, INJECTION + '\n\n' + MARKER, 1)
