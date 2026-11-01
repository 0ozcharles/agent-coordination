from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, Table, TableStyle

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output/pdf/multi_agent_architecture_research_report_zh.pdf'
OUT.parent.mkdir(parents=True, exist_ok=True)
pdfmetrics.registerFont(TTFont('Song', '/System/Library/Fonts/Supplemental/Songti.ttc', subfontIndex=6))
pdfmetrics.registerFont(TTFont('SongB', '/System/Library/Fonts/Supplemental/Songti.ttc', subfontIndex=1))
pdfmetrics.registerFont(TTFont('Hei', '/System/Library/Fonts/STHeiti Medium.ttc', subfontIndex=1))
pdfmetrics.registerFontFamily('Song', normal='Song', bold='SongB', italic='Song', boldItalic='SongB')
W,H=A4
M=55
CW=W-2*M
INK=colors.HexColor('#171717')
GREY=colors.HexColor('#555555')
LINE=colors.HexColor('#B9B9B9')
styles={
    'body':ParagraphStyle('body',fontName='Song',fontSize=11,leading=19.5,textColor=INK,wordWrap='CJK',firstLineIndent=22),
    'lead':ParagraphStyle('lead',fontName='Song',fontSize=10.5,leading=18.5,textColor=INK,wordWrap='CJK'),
    'h1':ParagraphStyle('h1',fontName='Hei',fontSize=14,leading=22,textColor=INK,wordWrap='CJK'),
    'h2':ParagraphStyle('h2',fontName='Hei',fontSize=11.5,leading=19,textColor=INK,wordWrap='CJK'),
    'table':ParagraphStyle('table',fontName='Song',fontSize=9.8,leading=16,textColor=INK,wordWrap='CJK'),
    'th':ParagraphStyle('th',fontName='SongB',fontSize=10,leading=16,textColor=INK,wordWrap='CJK'),
    'caption':ParagraphStyle('caption',fontName='Song',fontSize=9.4,leading=15,textColor=GREY,wordWrap='CJK',alignment=1),
    'small':ParagraphStyle('small',fontName='Song',fontSize=9.2,leading=15.5,textColor=GREY,wordWrap='CJK'),
    'ref':ParagraphStyle('ref',fontName='Song',fontSize=9.7,leading=15.8,textColor=INK,wordWrap='CJK'),
}
c=canvas.Canvas(str(OUT),pagesize=A4,pageCompression=1)
c.setTitle('多智能体系统的架构优化与失败修复：文献调研')
c.setAuthor('')
c.setSubject('多智能体工作流、通信拓扑、动态结构调整与失败修复的相关研究')
c.setCreator('ReportLab')
y=0
page=0
bottoms=[]

def ref(n):
    return f'<link href="#ref{n}">[{n}]</link>'

def p(text,style='body',gap=10):
    global y
    obj=Paragraph(text,styles[style])
    _,h=obj.wrap(CW,1000)
    if y-h<65:
        raise ValueError(f'Page {page} overflow: bottom {y-h:.1f}, {text[:50]}')
    obj.drawOn(c,M,y-h)
    y-=h+gap

def section(text):
    p(text,'h1',gap=13)

def sub(text):
    p(text,'h2',gap=8)

def begin(bookmark):
    global page,y
    page+=1
    c.bookmarkPage(f'page{page}')
    c.addOutlineEntry(bookmark,f'page{page}',0,False)
    y=H-56

def finish():
    bottoms.append(round(y,1))
    c.setFillColor(GREY)
    c.setFont('Song',9)
    c.drawCentredString(W/2,35,str(page))
    c.showPage()

def table(caption,heads,rows,widths):
    global y
    p(caption,'caption',gap=6)
    data=[[Paragraph(x,styles['th']) for x in heads]]
    data.extend([[Paragraph(x,styles['table']) for x in row] for row in rows])
    t=Table(data,colWidths=widths,hAlign='LEFT')
    t.setStyle(TableStyle([
        ('VALIGN',(0,0),(-1,-1),'TOP'),
        ('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),
        ('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8),
        ('LINEABOVE',(0,0),(-1,0),.8,INK),
        ('LINEBELOW',(0,0),(-1,0),.6,INK),
        ('LINEBELOW',(0,1),(-1,-2),.35,LINE),
        ('LINEBELOW',(0,-1),(-1,-1),.8,INK),
    ]))
    _,h=t.wrap(CW,1000)
    if y-h<65:
        raise ValueError(f'Table overflow on {page}: {y-h:.1f}')
    t.drawOn(c,M,y-h)
    y-=h+16

# Page 1: standard report title and overview.
begin('研究概况')
c.setFillColor(INK)
c.setFont('Hei',19)
c.drawCentredString(W/2,y-22,'多智能体系统的架构优化与失败修复')
c.setFont('Song',12)
c.drawCentredString(W/2,y-47,'文献调研')
y-=78
p(f'<b>摘要：</b>多智能体系统的组织方式涉及任务分工、通信连接、执行顺序与信息共享。相关研究已从预先设计工作流，扩展到任务条件化的拓扑生成和执行过程中的结构调整。本文选取六项相关工作，梳理其研究对象、方法与评估方式，涵盖自动设计、动态协作、失败分类和修复评估四个方面。不同方法中的“结构优化”具有不同含义，其实验结果也依赖各自的任务、模型和预算设置。{ref(1)}-{ref(6)}','lead',gap=12)
p('<b>关键词：</b>多智能体系统；工作流优化；通信拓扑；共享记忆；失败修复','small',gap=22)
section('1  研究概况')
p('本报告关注基于大语言模型的多智能体系统。这里的架构包括智能体的角色与分工、消息传递关系、执行流程，以及共享信息的组织方式。文献按主要优化对象与调整时机归类，见表 1。',gap=15)
table('表 1  相关研究概览',['研究方向','代表工作','主要研究对象'],[
    ['工作流自动设计',f'AFlow {ref(1)}','以代码表示的工作流与控制逻辑'],
    ['任务条件化拓扑',f'G-Designer {ref(2)}','针对任务生成智能体通信关系'],
    ['运行中结构调整',f'MANTA {ref(3)}','依据协作记录修改组织结构'],
    ['协作与记忆联合演化',f'EvoHyper {ref(4)}','协作组及其共享记忆'],
    ['失败分类',f'MAST {ref(5)}','失败类型与执行轨迹标注'],
    ['修复评估',f'Repair or Resample? {ref(6)}','失败重现、局部干预与完整重跑'],
],[112,135,CW-247])
p('其中，工作流搜索解决可复用流程的设计问题；任务条件化方法为输入生成结构；运行中调整则使用已经产生的执行信息修改后续协作。失败分析与回放评估为这些系统提供诊断和检验工具。',gap=0)
finish()

# Page 2: workflow and topology generation.
begin('工作流与通信拓扑的自动设计')
section('2  工作流与通信拓扑的自动设计')
sub('2.1  AFlow：以代码为表示的工作流搜索')
p(f'AFlow 将智能体工作流表示为代码：节点对应模型调用，连接及控制逻辑定义各步骤的依赖关系。大语言模型生成修改方案，蒙特卡洛树搜索负责选择、扩展并评价候选工作流。{ref(1)}')
p('候选方案在验证集上执行，结果与修改历史被保存为经验，用于后续搜索。最终输出是一套经过选择的工作流。因此，其主要优化发生在测试任务执行前，而非对每次运行中的故障进行局部修复。')
p(f'论文在数学、问答和代码生成共六个基准上评估。数学任务使用解题率，问答使用 F1，代码生成使用 pass@1；这些指标反映不同任务的结果质量。{ref(1)}',gap=19)
sub('2.2  G-Designer：根据任务生成通信拓扑')
p(f'G-Designer 将智能体及任务信息编码为图表示，通过变分图自编码器生成通信连接。它根据任务需求选择协作结构，同时控制通信开销；目标函数兼顾任务表现、参考拓扑约束与稀疏性。{ref(2)}')
p('训练使用部分任务，测试时固定学习到的参数，为新的输入生成拓扑。其任务自适应性主要体现在输入与通信结构之间的对应关系，区别于依据中途失败记录反复重写结构。')
p(f'论文在 MMLU、GSM8K、MultiArith、SVAMP、AQuA 和 HumanEval 上进行实验，并分析性能、token 消耗与对抗扰动下的表现。{ref(2)}',gap=17)
table('表 2  两类自动设计方法的区别',['比较项','AFlow','G-Designer'],[
    ['优化表示','代码工作流','智能体通信图'],
    ['主要机制','搜索候选代码并执行评价','学习任务到通信结构的映射'],
    ['输出','经搜索选定的工作流','面向当前任务的拓扑'],
],[86,(CW-86)/2,(CW-86)/2])
p('两项工作分别从流程表示和连接关系入手，将协作架构作为可优化对象；它们关注的结构粒度并不相同。','small',gap=0)
finish()

# Page 3: runtime changes with precise merge semantics.
begin('运行过程中的结构调整')
section('3  运行过程中的结构调整')
sub('3.1  MANTA：基于执行记录的拓扑修复')
p(f'MANTA 在任务开始时规划初始拓扑，在执行中检查协作记录，并在发现结构性问题时进行有限次数的修改。可修改对象包括角色、通信连接、执行顺序、信息可见范围和验证路径；底层模型权重保持不变。{ref(3)}')
p('系统将拓扑规划、过程审查和经验整理分开处理，并用控制代码限制修复与终止过程。短期经验服务于当前运行，长期经验用于后续任务。过程审查使用可观察的协作信号，而不是测试集的最终正确性标签。')
p(f'其评估覆盖信息检索、工具使用、规划、工作流执行与数学推理五类基准。该工作的主要特点是在任务执行过程中调整组织结构。{ref(3)}',gap=19)
sub('3.2  EvoHyper：协作结构与共享记忆的联合演化')
p(f'EvoHyper 使用超图表示协作关系：一个超边连接一组智能体，并携带该组的共享记忆。控制器通过 Update、Spawn 和 Merge 修改这些协作单元，使信息组织与通信结构一起变化。{ref(4)}',gap=12)
table('表 3  EvoHyper 的演化操作',['操作','作用'],[
    ['Update','整合新输出，更新已有协作单元的记忆。'],
    ['Spawn','为新的协作需求建立协作单元。'],
    ['Merge','合并相关协作单元的成员集合，并融合、压缩共享记忆。'],
],[94,CW-94])
p(f'Merge 保留参与协作的智能体，将多个组整合成一个更高阶的组；它不等同于把多个子任务集中交给单一智能体执行。论文在推理与代码生成基准上评估，并通过消融比较控制器、记忆机制和合并操作的贡献。{ref(4)}')
p('MANTA 主要围绕过程异常调整角色与协作流程；EvoHyper 的调整单位是同时承载成员关系和记忆的协作组。二者都在运行中改变结构，但修改对象和控制方式不同。','small',gap=0)
finish()

# Page 4: diagnostic and evaluation evidence.
begin('失败分析与修复评估')
section('4  失败分析与修复评估')
sub('4.1  MAST：多智能体失败分类')
p(f'Why Do Multi-Agent LLM Systems Fail? 提出 MAST 失败分类，并构建 MAST-Data。论文修订版收集了来自七个框架的 1,600 余条标注轨迹；分类依据对 150 条轨迹的专家分析形成，共包含 14 种失败模式。{ref(5)}',gap=12)
table('表 4  MAST 的三类失败',['类别','代表性问题'],[
    ['系统设计问题','未遵循任务或角色要求、步骤重复、对话历史丢失。'],
    ['智能体间失配','信息未充分传递、忽略其他智能体输入、任务偏离。'],
    ['任务验证问题','提前终止、验证缺失或不完整、错误验证。'],
],[124,CW-124])
p(f'该工作提供了描述与分析失败的共同分类，并展示角色及协作干预案例。其主要产物是失败数据和分类工具，不是一套自动生成最优架构的策略。{ref(5)}',gap=19)
sub('4.2  Repair or Resample?：区分局部修复与随机重试')
p(f'该研究提出 SymTrace，通过重建已记录的执行前缀，从指定干预点恢复后续运行；同时构建含 536 条人工标注失败轨迹的 SymFail 数据集，在 AG2、CrewAI 和 Magentic-One 上比较修复方法。{ref(6)}')
p('论文报告：基于异常迹象定位的单次节点干预修复率为 20.15%，无引导完整重跑在最多三次尝试下为 6.90%。该结果来自其特定失败集合，比较的是不同尝试预算下的修复表现，并非严格等 token 预算的结果。')
p(f'受控回放使干预前的记录具有更好的可比性，但后续模型采样、工具响应和评价误差仍会影响结果。因此，回放前缀一致不等于端到端结果完全确定。{ref(6)}',gap=16)
p('这两项工作分别回答“系统出现了什么失败”和“干预是否改善了既有失败”。它们与架构优化方法互补，也说明任务成功率、失败类型及修复率是不同层面的评价指标。','small',gap=0)
finish()

# Page 5: short objective comparison plus bibliographic references.
begin('文献比较与参考文献')
section('5  文献比较')
p(f'现有研究覆盖三种不同的结构调整时机：AFlow 在验证任务上搜索工作流；G-Designer 为输入任务生成拓扑；MANTA 与 EvoHyper 利用执行过程中的信息改变协作结构。前两者侧重设计，后两者将结构纳入运行时控制。{ref(1)}-{ref(4)}')
p(f'结构调整的含义也存在差异，包括修改控制逻辑、重连通信边、变更角色，以及合并协作组与共享记忆。特别是 EvoHyper 中的协作组合并，不能直接理解为减少执行智能体的数量。{ref(4)}')
p(f'各论文采用的模型、任务集合、基线和成本口径不同，报告的提升幅度不宜直接横向排序。MAST 的分类与 SymTrace 的回放分别提供失败描述和干预评估视角，使结构变化与任务结果之间的关系能够被更具体地分析。{ref(5)} {ref(6)}',gap=18)
section('参考文献')
references=[
    (1,'Zhang J, Xiang J, Yu Z, et al.','AFlow: Automating Agentic Workflow Generation.','arXiv:2410.10762, 2024（v4, 2025）.','https://arxiv.org/abs/2410.10762'),
    (2,'Zhang G, Yue Y, Sun X, et al.','G-Designer: Architecting Multi-agent Communication Topologies via Graph Neural Networks.','arXiv:2410.11782, 2024（v3, 2025）.','https://arxiv.org/abs/2410.11782'),
    (3,'Huang M, Wang J, Lai Y, et al.','MANTA: Multi-Agent Network Topology Adaptation for Self-Evolving Multi-Agent Systems.','arXiv:2607.28527, 2026，预印本.','https://arxiv.org/abs/2607.28527'),
    (4,'Zhang H, Zhong Y, Gan L, et al.','EvoHyper: Evolving Hypergraph Topologies for Unified Collaboration in Multi-Agent Communication.','Findings of ACL 2026: 25130-25143.','https://aclanthology.org/2026.findings-acl.1258/'),
    (5,'Cemri M, Pan M Z, Yang S, et al.','Why Do Multi-Agent LLM Systems Fail?','arXiv:2503.13657, 2025（v3）.','https://arxiv.org/abs/2503.13657'),
    (6,'Luan Z, Zhang X, Hu M, et al.','Repair or Resample? Rethinking Failure Debugging in LLM Multi-Agent Systems.','arXiv:2608.25920, 2026（v2），预印本.','https://arxiv.org/abs/2608.25920'),
]
for n,authors,title,details,url in references:
    c.bookmarkHorizontalAbsolute(f'ref{n}',y)
    p(f'[{n}] {escape(authors)} {escape(title)} {escape(details)}<br/><link href="{url}" color="#333333">{url}</link>','ref',gap=9)
p('文献检索截至 2026 年 10 月 28 日。本文为六项相关工作的定向调研，实验数据引自原论文。','small',gap=0)
finish()
assert page==5
c.save()
print(OUT)
print('Pages:',page,'Content bottoms:',bottoms)
