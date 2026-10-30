/** Bundled Markdown → sanitized HTML → KaTeX, shared by both runtimes. */
(() => {
  window.ICS = window.ICS || {};
  const escape = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  // CommonMark treats Chinese punctuation before a closing ** as an
  // intraword delimiter when the following character is Chinese. Model output
  // frequently uses exactly this form: **判断。**他接着说。 Recognize the pair
  // as an inline token, leaving code, escapes, HTML and sanitization to marked.
  marked.use({extensions:[{
    name:'cjkStrong',level:'inline',
    start(source){return source.indexOf('**');},
    tokenizer(source){
      if(!source.startsWith('**') || source[2]==='*')return;
      for(let index=2;index<source.length-1;index++){
        if(source[index]==='\\'){index++;continue;}
        if(source[index]==='\n' && /^\n[ \t]*\n/.test(source.slice(index)))return;
        if(source[index]==='`'){
          const run=source.slice(index).match(/^`+/)[0];
          const end=source.indexOf(run,index+run.length);
          if(end!==-1){index=end+run.length-1;continue;}
        }
        if(source.startsWith('**',index) && source[index-1]!=='*' && source[index+2]!=='*'){
          const content=source.slice(2,index).trim();
          if(!content || !/\p{Script=Han}/u.test(content))return;
          return {type:'strong',raw:source.slice(0,index+2),text:content,tokens:this.lexer.inlineTokens(content)};
        }
      }
    },
  }]});
  function stashFormulas(value) {
    const formulas=[], codes=[];
    const mathToken=index=>`\uE000M${index}\uE001`;
    // Code is restored before marked parses it, so examples containing dollar
    // signs or LaTeX delimiters remain literal. Fence length is respected.
    let text=String(value).replace(/^( {0,3})(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:^\1\2[\t ]*$|(?![\s\S]))/gm, code=>{
      const token=`\uE000C${codes.length}\uE001`; codes.push(code); return token;
    });
    text=text.replace(/(`+)([^`]|(?!\1)`)*?\1/g, code=>{
      const token=`\uE000C${codes.length}\uE001`; codes.push(code); return token;
    });
    const stash=value=>{const token=mathToken(formulas.length);formulas.push(value);return token;};
    text=text.replace(/(?<!\\)(\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\])/g, match=>stash(match));
    text=text.replace(/(?<!\\)\$\$([\s\S]+?)\$\$/g,(_,formula)=>stash(`\\[${formula}\\]`));
    text=text.replace(/(?<![\\$])\$(?!\s)([^$\n]*?\S)\$(?![\d$])/g,(_,formula)=>stash(`\\(${formula}\\)`));
    text=text.replace(/\uE000C(\d+)\uE001/g,(_,index)=>codes[Number(index)]);
    return {text,formulas};
  }
  function renderMarkdown(value) {
    if (!value) return '';
    const stashed=stashFormulas(value);
    let html=marked.parse(stashed.text,{gfm:true,breaks:false});
    html=html.replace(/\uE000M(\d+)\uE001/g,(_,index)=>escape(stashed.formulas[Number(index)] || ''));
    const safe=DOMPurify.sanitize(html,{
      FORBID_TAGS:['iframe','object','embed','form','style','video','audio'],
      FORBID_ATTR:['style','srcset'],
    });
    const template=document.createElement('template');template.innerHTML=safe;
    template.content.querySelectorAll('input').forEach(input=>{
      if (input.type!=='checkbox') {input.remove();return;}
      const box=document.createElement('span');box.className='markdown-checkbox';
      if (input.parentElement?.tagName==='LI') input.parentElement.classList.add('markdown-task-item');
      box.setAttribute('role','checkbox');box.setAttribute('aria-checked',String(input.checked));
      box.textContent=input.checked ? '☑' : '☐';input.replaceWith(box);
    });
    template.content.querySelectorAll('img').forEach(img=>{
      const src=img.getAttribute('src') || '';
      // Markdown is untrusted. Permit ordinary web and embedded bitmap images;
      // SVG data, file paths and executable schemes never become image sources.
      if (!/^(https?:\/\/|\/\/|data:image\/(?:png|jpe?g|gif|webp);base64,)/i.test(src)) {
        const label=document.createElement('span');label.className='markdown-image-missing';
        label.textContent=`[图片：${img.alt || '未附带'}]`;
        label.title='导入时可同时选择 Markdown 和文中引用的图片';img.replaceWith(label);return;
      }
      img.loading='lazy';img.decoding='async';img.referrerPolicy='no-referrer';
      img.removeAttribute('width');img.removeAttribute('height');
    });
    template.content.querySelectorAll('a').forEach(link=>{
      const href=link.getAttribute('href') || '';
      if (/^(?:file|javascript|data|vbscript):/i.test(href)) link.removeAttribute('href');
      link.rel='noopener noreferrer';
    });
    template.content.querySelectorAll('table').forEach(table=>{
      const wrapper=document.createElement('div');wrapper.className='table-wrap';wrapper.tabIndex=0;
      wrapper.setAttribute('role','region');wrapper.setAttribute('aria-label','表格，可横向滚动');
      table.replaceWith(wrapper);wrapper.append(table);
    });
    return template.innerHTML;
  }
  function activateKaTeX(element) {
    if (typeof renderMathInElement!=='function' || !element) return;
    renderMathInElement(element,{
      delimiters:[{left:'\\[',right:'\\]',display:true},{left:'\\(',right:'\\)',display:false}],
      throwOnError:false,
      ignoredTags:['script','noscript','style','textarea','pre','code','option'],
    });
  }
  function plainSnippet(value,maxLen=100) {
    const text=String(value || '').replace(/!\[[^\]]*\]\([^)]*\)/g,' ')
      .replace(/\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\$[^$\n]+?\$|\\\([\s\S]*?\\\)/g,' ')
      .replace(/#{1,6}\s+|[*_`~]/g,'').replace(/\[([^\]]+)\]\([^)]+\)/g,'$1')
      .replace(/[|:\-]+/g,' ').replace(/\s+/g,' ').trim();
    return text.length>maxLen ? text.slice(0,maxLen)+'…' : text;
  }
  window.ICS.render={renderMarkdown,activateKaTeX,plainSnippet};
})();
